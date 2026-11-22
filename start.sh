#!/usr/bin/env bash
set -euo pipefail

# GenX71 AutoVJ — Smart Docker Build & Start/Restart Script
#
# Checks whether either Docker image (genx71-autovj or tts-worker) needs to be
# rebuilt based on image presence and source-file SHA-256 fingerprints, rebuilds
# only what changed, and starts/restarts the stack.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

STATE_DIR="$REPO_DIR/cache/.docker-build-state"
APP_SERVICE="genx71-autovj"
APP_IMAGE="genx71-autovj:latest"
APP_CONTAINER="genx71-autovj"

TTS_SERVICE="tts-worker"
TTS_IMAGE="genx71-tts-worker:latest"
TTS_CONTAINER="genx71-tts-worker"

CHECK_ONLY=0
DO_PULL=0
FORCE_APP=0
FORCE_TTS=0
NO_CACHE=0
NO_RESTART=0

usage() {
  cat <<EOF
Usage: ./start.sh [OPTIONS]

Checks whether genx71-autovj and/or tts-worker need rebuilding, builds only
the container(s) that need it, and starts or restarts the Docker stack.

Options:
  -c, --check        Check build & container status only (dry run; no build/restart)
  -p, --pull         Run 'git pull --ff-only' before checking for rebuilds
  -f, --force-build  Force rebuild of both containers
      --build-app    Force rebuild of genx71-autovj only
      --build-tts    Force rebuild of tts-worker only
      --no-cache     Pass --no-cache when building images
      --no-restart   Do not restart already-running containers that were not rebuilt
  -h, --help         Show this help message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -c|--check)       CHECK_ONLY=1 ;;
    -p|--pull)        DO_PULL=1 ;;
    -f|--force-build) FORCE_APP=1; FORCE_TTS=1 ;;
    --build-app)      FORCE_APP=1 ;;
    --build-tts)      FORCE_TTS=1 ;;
    --no-cache)       NO_CACHE=1 ;;
    --no-restart)     NO_RESTART=1 ;;
    -h|--help)        usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Run './start.sh --help' for usage." >&2
      exit 1
      ;;
  esac
  shift
done

# Detect Docker & Docker Compose
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: 'docker' is not installed or not on PATH." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running or current user lacks permission." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: Neither 'docker compose' nor 'docker-compose' was found." >&2
  exit 1
fi

if [ "$DO_PULL" -eq 1 ]; then
  echo "==> Pulling latest changes from git..."
  git pull --ff-only
fi

sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

# List all relevant build input files for a given service (sorted, relative paths)
list_service_files() {
  local svc="$1"
  if [ "$svc" = "$TTS_SERVICE" ]; then
    find tts_worker -type f \
      ! -path '*/__pycache__/*' \
      ! -name '*.pyc' \
      ! -name '*.pyo' \
      ! -name '.DS_Store' \
      2>/dev/null | LC_ALL=C sort
  else
    {
      for f in Dockerfile requirements.txt daemon.py genx.jpg genxlogofinal.png; do
        [ -f "$f" ] && printf '%s\n' "$f"
      done
      for d in api core web assets; do
        if [ -d "$d" ]; then
          find "$d" -type f \
            ! -path '*/__pycache__/*' \
            ! -name '*.pyc' \
            ! -name '*.pyo' \
            ! -name '.DS_Store' \
            2>/dev/null
        fi
      done
    } | LC_ALL=C sort -u
  fi
}

# Compute deterministic SHA-256 fingerprint of all build input files for a service
compute_fingerprint() {
  local svc="$1"
  local file
  {
    while IFS= read -r file; do
      [ -n "$file" ] || continue
      if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file"
      else
        shasum -a 256 "$file"
      fi
    done < <(list_service_files "$svc")
  } | sha256_stream
}

# Get newest mtime (unix epoch) among a service's build input files
newest_file_mtime() {
  local svc="$1"
  local newest=0 mtime file
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    mtime=$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null || echo 0)
    if [ "$mtime" -gt "$newest" ]; then
      newest="$mtime"
    fi
  done < <(list_service_files "$svc")
  echo "$newest"
}

# Get Docker image creation time as unix epoch
image_created_epoch() {
  local image="$1"
  local created
  created=$(docker image inspect --format '{{.Created}}' "$image" 2>/dev/null || true)
  if [ -z "$created" ]; then
    echo 0
    return
  fi
  date -d "$created" +%s 2>/dev/null || python3 -c "
from datetime import datetime
import sys
s = sys.argv[1].replace('Z', '+00:00')
# Truncate fractional seconds beyond microseconds if present
if '.' in s:
    head, rest = s.split('.', 1)
    tz_idx = max(rest.find('+'), rest.find('-'))
    if tz_idx != -1:
        frac, tz = rest[:tz_idx], rest[tz_idx:]
        s = f'{head}.{frac[:6]}{tz}'
print(int(datetime.fromisoformat(s).timestamp()))
" "$created" 2>/dev/null || echo 0
}

save_build_state() {
  local svc="$1" image="$2" fp="$3"
  local img_id
  img_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
  mkdir -p "$STATE_DIR"
  printf '%s\n%s\n' "$fp" "$img_id" > "$STATE_DIR/${svc}.state"
}

# Returns 0 if rebuild is needed (and sets BUILD_REASON), 1 if up-to-date
BUILD_REASON=""
needs_build() {
  local svc="$1" image="$2" force="$3" current_fp="$4"
  BUILD_REASON=""

  if [ "$force" -eq 1 ]; then
    BUILD_REASON="forced via CLI flag"
    return 0
  fi

  local img_id
  img_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
  if [ -z "$img_id" ]; then
    BUILD_REASON="image '$image' not found locally"
    return 0
  fi

  local state_file="$STATE_DIR/${svc}.state"
  if [ -f "$state_file" ]; then
    local saved_fp saved_id
    saved_fp=$(sed -n '1p' "$state_file" 2>/dev/null || true)
    saved_id=$(sed -n '2p' "$state_file" 2>/dev/null || true)
    if [ "$current_fp" != "$saved_fp" ]; then
      BUILD_REASON="source files changed since last build"
      return 0
    fi
    if [ "$img_id" != "$saved_id" ]; then
      BUILD_REASON="local image ID changed externally"
      return 0
    fi
    BUILD_REASON="up-to-date (matches last build fingerprint)"
    return 1
  fi

  # First run on an existing host with no recorded state file yet:
  # fall back to comparing newest source file mtime against image creation time.
  local img_epoch src_epoch
  img_epoch=$(image_created_epoch "$image")
  src_epoch=$(newest_file_mtime "$svc")
  if [ "$src_epoch" -gt "$img_epoch" ]; then
    BUILD_REASON="source files are newer than existing image"
    return 0
  fi

  BUILD_REASON="up-to-date (existing image is newer than source files)"
  if [ "$CHECK_ONLY" -eq 0 ]; then
    save_build_state "$svc" "$image" "$current_fp"
  fi
  return 1
}

container_state_sig() {
  local cname="$1"
  docker inspect --format '{{.State.Running}} {{.Id}} {{.State.StartedAt}}' "$cname" 2>/dev/null || echo "missing"
}

echo "==> Checking Docker build status for GenX71 AutoVJ..."

APP_FP=$(compute_fingerprint "$APP_SERVICE")
TTS_FP=$(compute_fingerprint "$TTS_SERVICE")

APP_NEEDS_BUILD=0
if needs_build "$APP_SERVICE" "$APP_IMAGE" "$FORCE_APP" "$APP_FP"; then
  APP_NEEDS_BUILD=1
fi
APP_REASON="$BUILD_REASON"

TTS_NEEDS_BUILD=0
if needs_build "$TTS_SERVICE" "$TTS_IMAGE" "$FORCE_TTS" "$TTS_FP"; then
  TTS_NEEDS_BUILD=1
fi
TTS_REASON="$BUILD_REASON"

printf '  - %-16s : %s (%s)\n' \
  "$APP_SERVICE" \
  "$([ "$APP_NEEDS_BUILD" -eq 1 ] && echo 'REBUILD NEEDED' || echo 'OK')" \
  "$APP_REASON"
printf '  - %-16s : %s (%s)\n' \
  "$TTS_SERVICE" \
  "$([ "$TTS_NEEDS_BUILD" -eq 1 ] && echo 'REBUILD NEEDED' || echo 'OK')" \
  "$TTS_REASON"

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo ""
  echo "==> Current container status:"
  "${COMPOSE[@]}" ps || true
  exit 0
fi

# Build only the container(s) that need it
BUILD_ARGS=()
if [ "$NO_CACHE" -eq 1 ]; then
  BUILD_ARGS+=(--no-cache)
fi

if [ "$APP_NEEDS_BUILD" -eq 1 ]; then
  echo ""
  echo "==> Building $APP_SERVICE..."
  "${COMPOSE[@]}" build "${BUILD_ARGS[@]}" "$APP_SERVICE"
  save_build_state "$APP_SERVICE" "$APP_IMAGE" "$APP_FP"
fi

if [ "$TTS_NEEDS_BUILD" -eq 1 ]; then
  echo ""
  echo "==> Building $TTS_SERVICE..."
  "${COMPOSE[@]}" build "${BUILD_ARGS[@]}" "$TTS_SERVICE"
  save_build_state "$TTS_SERVICE" "$TTS_IMAGE" "$TTS_FP"
fi

# Snapshot pre-existing running container signatures so we know which ones
# 'docker compose up -d' recreated vs left untouched.
PRE_APP_SIG=$(container_state_sig "$APP_CONTAINER")
PRE_TTS_SIG=$(container_state_sig "$TTS_CONTAINER")

echo ""
echo "==> Starting / updating Docker Compose stack..."
"${COMPOSE[@]}" up -d --remove-orphans

if [ "$NO_RESTART" -eq 0 ]; then
  POST_APP_SIG=$(container_state_sig "$APP_CONTAINER")
  POST_TTS_SIG=$(container_state_sig "$TTS_CONTAINER")

  TO_RESTART=()
  if [[ "$PRE_APP_SIG" == true\ * ]] && [ "$PRE_APP_SIG" = "$POST_APP_SIG" ]; then
    TO_RESTART+=("$APP_SERVICE")
  fi
  if [[ "$PRE_TTS_SIG" == true\ * ]] && [ "$PRE_TTS_SIG" = "$POST_TTS_SIG" ]; then
    TO_RESTART+=("$TTS_SERVICE")
  fi

  if [ "${#TO_RESTART[@]}" -gt 0 ]; then
    echo "==> Restarting unchanged running container(s): ${TO_RESTART[*]}..."
    "${COMPOSE[@]}" restart "${TO_RESTART[@]}"
  fi
fi

echo ""
echo "==> Container status:"
"${COMPOSE[@]}" ps

PORT="${PORT:-8710}"
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
TS_IP=$(tailscale ip -4 2>/dev/null | head -n 1 || true)

echo ""
echo "=========================================="
echo "  GenX71 AutoVJ Stack Ready"
echo "  Linux Desktop : http://localhost:${PORT}"
echo "  LAN (Wi-Fi)   : http://${LOCAL_IP}:${PORT}"
if [ -n "${TS_IP}" ]; then
  echo "  Tailscale     : http://${TS_IP}:${PORT}"
fi
echo "  Logs          : docker compose logs -f"
echo "=========================================="
