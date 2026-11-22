"""
Voice Profile Management & Regional Zero-Shot TTS Client for GenX71 AutoVJ.

Manages two regional voice profiles stored on disk:
  - data/voices/uk/ : British regional voice reference clip(s) (default)
  - data/voices/us/ : American voice reference clip(s) (auto-selected for US chart tracks)

Communicates with the local CPU Coqui XTTS-v2 sidecar (TTS_WORKER_URL, default
http://tts-worker:8711) to synthesize 5-8s spoken DJ hooks.
"""

import os
import re
import time
import wave
import logging
import subprocess
from typing import Any, Dict, List, Optional

import numpy as np
import requests

from core.db import get_setting

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICES_ROOT = os.path.join(PROJECT_ROOT, "data", "voices")
TTS_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "tts")

VALID_REGIONS = ("uk", "us")
DEFAULT_TTS_WORKER_URL = os.environ.get("TTS_WORKER_URL", "http://tts-worker:8711")
ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".aac", ".mp4"}


def _safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or "voice"))[0]
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_")[:48]
    return cleaned or "voice"


def get_tts_worker_url(db_path: Optional[str] = None) -> str:
    """Resolve the TTS worker base URL from settings or environment."""
    try:
        from_db = (get_setting("tts_worker_url", "", db_path=db_path) or "").strip()
        if from_db:
            return from_db.rstrip("/")
    except Exception:
        pass
    return (os.environ.get("TTS_WORKER_URL") or DEFAULT_TTS_WORKER_URL).rstrip("/")


def get_voice_dir(region: str, voices_root: Optional[str] = None) -> str:
    reg = (region or "").strip().lower()
    if reg not in VALID_REGIONS:
        raise ValueError(f"Invalid voice region '{region}'. Must be one of: {', '.join(VALID_REGIONS)}")
    root = voices_root or VOICES_ROOT
    d = os.path.join(root, reg)
    os.makedirs(d, exist_ok=True)
    return d


def _wav_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate() or 22050
            return round(wf.getnframes() / float(sr), 2)
    except Exception:
        return 0.0


def list_voice_clips(region: str, voices_root: Optional[str] = None) -> List[Dict[str, Any]]:
    d = get_voice_dir(region, voices_root=voices_root)
    clips: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(d)):
        if not fname.lower().endswith(".wav"):
            continue
        full = os.path.join(d, fname)
        if not os.path.isfile(full):
            continue
        clips.append({
            "filename": fname,
            "path": full,
            "size_bytes": os.path.getsize(full),
            "duration": _wav_duration(full),
        })
    return clips


def list_voice_profiles(voices_root: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    profiles: Dict[str, Dict[str, Any]] = {}
    for reg in VALID_REGIONS:
        clips = list_voice_clips(reg, voices_root=voices_root)
        profiles[reg] = {
            "region": reg,
            "label": "UK British Voice" if reg == "uk" else "US American Voice",
            "ready": len(clips) > 0,
            "clip_count": len(clips),
            "total_duration": round(sum(c["duration"] for c in clips), 2),
            "clips": clips,
        }
    return profiles


def has_any_voice_samples(voices_root: Optional[str] = None) -> bool:
    profiles = list_voice_profiles(voices_root=voices_root)
    return any(p["ready"] for p in profiles.values())


def save_voice_sample(
    region: str,
    original_filename: str,
    raw_bytes: bytes,
    voices_root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convert an uploaded audio clip (WAV/MP3/M4A/OGG/WEBM/etc.) into a clean,
    normalized 22.05kHz mono 16-bit PCM WAV suitable for XTTS-v2 zero-shot cloning.
    """
    if not raw_bytes or len(raw_bytes) < 256:
        raise ValueError("Uploaded audio file is empty or too small.")

    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext and ext not in ALLOWED_AUDIO_EXTS:
        raise ValueError(f"Unsupported audio format '{ext}'. Upload WAV, MP3, M4A, OGG, FLAC, or WEBM.")

    target_dir = get_voice_dir(region, voices_root=voices_root)
    ts = int(time.time())
    stem = _safe_stem(original_filename)
    tmp_in = os.path.join(target_dir, f".upload_{ts}_{stem}{ext or '.bin'}")
    out_name = f"{stem}_{ts}.wav"
    out_path = os.path.join(target_dir, out_name)

    try:
        with open(tmp_in, "wb") as f:
            f.write(raw_bytes)

        # Normalize to clean 22050Hz mono PCM WAV with gentle rumble filter + loudnorm
        af = "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=9"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", tmp_in,
                "-af", af,
                "-ar", "22050",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-t", "35",  # Cap reference clips at 35s for optimal XTTS-v2 conditioning
                out_path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        dur = _wav_duration(out_path)
        if dur < 1.5:
            if os.path.exists(out_path):
                os.remove(out_path)
            raise ValueError("Voice sample is too short (< 1.5s). Please upload a 6–30 second clean voice clip.")

        return {
            "region": region.lower(),
            "filename": out_name,
            "path": out_path,
            "size_bytes": os.path.getsize(out_path),
            "duration": dur,
        }
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="ignore")[-300:]
        raise ValueError(f"FFmpeg could not decode audio file: {err}") from exc
    finally:
        if os.path.exists(tmp_in):
            try:
                os.remove(tmp_in)
            except OSError:
                pass


def delete_voice_sample(region: str, filename: str, voices_root: Optional[str] = None) -> bool:
    d = get_voice_dir(region, voices_root=voices_root)
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.lower().endswith(".wav"):
        raise ValueError("Invalid voice clip filename.")
    target = os.path.join(d, safe_name)
    if os.path.exists(target):
        os.remove(target)
        return True
    return False


def resolve_voice_region(
    track_chart_region: Optional[str] = None,
    requested_region: Optional[str] = None,
    voices_root: Optional[str] = None,
) -> Optional[str]:
    """
    Pick the regional voice profile to use:
      1. If requested_region is 'uk' or 'us' and has clips -> use it.
      2. Otherwise auto-route by track_chart_region ('US' -> 'us', else 'uk').
      3. Fall back to whichever region has uploaded clips ('uk' preferred).
      4. Return None if no voice samples are uploaded at all.
    """
    profiles = list_voice_profiles(voices_root=voices_root)
    req = (requested_region or "").strip().lower()
    if req in VALID_REGIONS and profiles[req]["ready"]:
        return req

    chart_reg = (track_chart_region or "UK").strip().upper()
    preferred = "us" if chart_reg == "US" else "uk"
    fallback = "uk" if preferred == "us" else "us"

    if profiles[preferred]["ready"]:
        return preferred
    if profiles[fallback]["ready"]:
        return fallback
    return None


def get_tts_worker_health(worker_url: Optional[str] = None, db_path: Optional[str] = None, timeout: float = 2.5) -> Dict[str, Any]:
    url = (worker_url or get_tts_worker_url(db_path=db_path)).rstrip("/")
    try:
        resp = requests.get(f"{url}/health", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return {
            "online": True,
            "url": url,
            "engine": data.get("engine", "xtts_v2"),
            "model_loaded": bool(data.get("model_loaded", False)),
            "busy": bool(data.get("busy", False)),
            "device": data.get("device", "cpu"),
            "error": data.get("error"),
        }
    except Exception as exc:
        return {
            "online": False,
            "url": url,
            "engine": "xtts_v2",
            "model_loaded": False,
            "busy": False,
            "device": "cpu",
            "error": str(exc),
        }


def extract_voice_rms(wav_path: str, fps: int = 30) -> List[float]:
    """Extract normalized [0..1] per-frame voice amplitude envelope for the ON AIR meter."""
    try:
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            n_channels = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if n_channels > 1:
                samples = samples.reshape(-1, n_channels).mean(axis=1)
        hop = max(1, sr // fps)
        n_frames = len(samples) // hop
        if n_frames == 0:
            return []
        frames = samples[: n_frames * hop].reshape(n_frames, hop)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        p95 = float(np.percentile(rms, 95)) or 1.0
        norm = np.clip(rms / p95, 0.0, 1.0)
        smoothed = np.convolve(norm, np.ones(3) / 3.0, mode="same")
        return [round(float(x), 3) for x in smoothed]
    except Exception as exc:
        logger.warning("Failed to extract voice RMS from %s: %s", wav_path, exc)
        return []


def synthesize_hook_audio(
    text: str,
    region: str,
    output_path: str,
    speed: float = 1.05,
    voices_root: Optional[str] = None,
    worker_url: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 660,  # Up to 11 minutes for CPU synthesis
) -> Dict[str, Any]:
    """
    Request zero-shot voice cloning from the tts-worker sidecar.
    Returns dict: {"audio_path", "duration", "region", "elapsed_sec", "voice_rms"}.
    """
    clips = list_voice_clips(region, voices_root=voices_root)
    if not clips:
        raise RuntimeError(f"No reference voice clips uploaded for region '{region}'.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    url = (worker_url or get_tts_worker_url(db_path=db_path)).rstrip("/")
    payload = {
        "text": text,
        "speaker_wavs": [c["path"] for c in clips],
        "output_path": os.path.abspath(output_path),
        "language": "en",
        "speed": float(speed),
        "temperature": 0.72,
    }

    try:
        resp = requests.post(f"{url}/synthesize", json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"TTS worker unreachable at {url}: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"TTS worker error ({resp.status_code}): {detail}")

    data = resp.json()
    final_path = data.get("output_path") or output_path
    if not os.path.exists(final_path):
        raise RuntimeError(f"TTS worker reported success but output WAV missing at {final_path}")

    duration = float(data.get("duration") or _wav_duration(final_path))
    return {
        "audio_path": final_path,
        "duration": round(duration, 2),
        "region": region.lower(),
        "elapsed_sec": float(data.get("elapsed_sec", 0.0)),
        "voice_rms": extract_voice_rms(final_path, fps=30),
    }
