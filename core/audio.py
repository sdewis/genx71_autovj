"""
Intelligent Audio Sourcing and Boundary Clipper for GenX71 AutoVJ.
1. Fetches official 30s studio previews from Deezer & iTunes (with strict artist/title matching).
2. Falls back to yt-dlp YouTube audio extraction for rarer Top 21-40 wildcards.
3. Analyzes amplitude/onset envelopes with NumPy/SciPy to snap clip boundaries to
   verse/chorus phrasing and avoid abrupt mid-lyric cuts.
4. Outputs normalized 30s audio + per-frame RMS energy envelope for reactive visualizers.
"""

import os
import re
import wave
import logging
import subprocess
from typing import Optional, Tuple, List, Dict, Any
import requests
import numpy as np
from scipy.signal import find_peaks

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "audio")
HEADERS = {"User-Agent": "GenX71AutoVJ/2.0"}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")[:60]


def _match_score(target_artist: str, target_title: str, cand_artist: str, cand_title: str) -> int:
    score = 0
    ta, tt = target_artist.lower(), target_title.lower()
    ca, ct = cand_artist.lower(), cand_title.lower()

    if ta == ca:
        score += 5
    elif ta in ca or ca in ta:
        score += 3

    if tt == ct:
        score += 5
    elif tt in ct or ct in tt:
        score += 3

    # Penalize karaoke, tribute, cover, live versions unless requested
    for bad in ("karaoke", "tribute", "cover", "instrumental", "made famous"):
        if bad in ct or bad in ca:
            score -= 6
    return score


def fetch_deezer_preview(artist: str, title: str, timeout: int = 10) -> Optional[str]:
    """Search Deezer API for an exact track match and return its 30s MP3 preview URL."""
    try:
        q = f'artist:"{artist}" track:"{title}"'
        resp = requests.get("https://api.deezer.com/search", params={"q": q, "limit": 8}, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            resp = requests.get("https://api.deezer.com/search", params={"q": f"{artist} {title}", "limit": 8}, headers=HEADERS, timeout=timeout)
            data = resp.json().get("data", [])

        best_url = None
        best_score = 3  # Require reasonable match
        for item in data:
            preview = item.get("preview")
            if not preview:
                continue
            sc = _match_score(artist, title, item.get("artist", {}).get("name", ""), item.get("title", ""))
            if sc > best_score:
                best_score = sc
                best_url = preview
        return best_url
    except Exception as e:
        logger.warning(f"Deezer lookup failed for {artist} - {title}: {e}")
        return None


def fetch_itunes_preview(artist: str, title: str, timeout: int = 10) -> Optional[str]:
    """Search iTunes API for a 30s M4A preview URL."""
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "media": "music", "entity": "song", "limit": 8},
            headers=HEADERS,
            timeout=timeout
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])

        best_url = None
        best_score = 3
        for item in results:
            preview = item.get("previewUrl")
            if not preview:
                continue
            sc = _match_score(artist, title, item.get("artistName", ""), item.get("trackName", ""))
            if sc > best_score:
                best_score = sc
                best_url = preview
        return best_url
    except Exception as e:
        logger.warning(f"iTunes preview lookup failed for {artist} - {title}: {e}")
        return None


def fetch_youtube_audio(artist: str, title: str, out_raw_path: str) -> Optional[str]:
    """Fallback: Download audio from YouTube via yt-dlp."""
    try:
        query = f"ytsearch1:{artist} - {title} official audio"
        cmd = [
            "yt-dlp",
            "-f", "bestaudio/best",
            "--no-playlist",
            "-x", "--audio-format", "wav",
            "-o", out_raw_path,
            query
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        # yt-dlp might append .wav
        if os.path.exists(out_raw_path):
            return out_raw_path
        if os.path.exists(out_raw_path + ".wav"):
            return out_raw_path + ".wav"
        return None
    except Exception as e:
        logger.warning(f"yt-dlp fallback failed for {artist} - {title}: {e}")
        return None


def _load_wav_mono(wav_path: str) -> Tuple[np.ndarray, int]:
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)
        return samples, sr


MIN_CLIP_DURATION = 30.0
TARGET_CLIP_DURATION = 40.0
MIN_FULL_TRACK_CLIP = 35.0
MAX_FULL_TRACK_CLIP = 45.0
MAX_CLIP_DURATION = 55.0  # Safely below TikTok's 60s automated copyright mute threshold


def find_intelligent_clip_window(
    wav_path: str,
    target_duration: float = TARGET_CLIP_DURATION,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Analyze audio energy & phrase pauses to find the best hook window.
    - Default target is 40s (clamped 35-45s for full tracks, strictly <58s for TikTok's 60s limit).
    - If source is already <= target_duration + 1.5s (e.g. a 30s preview fallback),
      returns the full available duration up to max_duration.
    - If source is a full track, locates the highest-energy chorus/hook region
      and snaps start/end to local energy troughs (breath/phrase pauses).
    """
    samples, sr = _load_wav_mono(wav_path)
    total_dur = len(samples) / sr

    eff_min = min_duration if min_duration is not None else (
        MIN_FULL_TRACK_CLIP if target_duration >= MIN_FULL_TRACK_CLIP else max(1.0, target_duration * 0.8)
    )
    eff_max = max_duration if max_duration is not None else (
        min(MAX_CLIP_DURATION, max(MAX_FULL_TRACK_CLIP, target_duration + 5.0))
        if target_duration >= MIN_CLIP_DURATION
        else target_duration + 1.5
    )

    if total_dur <= target_duration + 1.5:
        return 0.0, round(min(total_dur, eff_max), 2)

    # Compute 50ms RMS energy envelope
    hop = int(sr * 0.05)
    n_hops = len(samples) // hop
    frames = samples[:n_hops * hop].reshape(n_hops, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-9)

    win_hops = int(target_duration / 0.05)
    if win_hops >= len(rms):
        return 0.0, round(min(total_dur, eff_max), 2)

    # Sliding window sum to find peak energy chorus/verse section
    # Avoid very first 10 seconds (often sparse intro) if track > 90s
    start_search_hop = int(10.0 / 0.05) if total_dur > 90.0 else 0
    end_search_hop = len(rms) - win_hops

    if end_search_hop <= start_search_hop:
        return 0.0, round(min(total_dur, eff_max), 2)

    best_idx = start_search_hop
    best_energy = -1.0
    for idx in range(start_search_hop, end_search_hop, 10):
        e = float(np.mean(rms[idx: idx + win_hops]))
        if e > best_energy:
            best_energy = e
            best_idx = idx

    # Snap start boundary to a local energy trough (breath/bar start) within +/- 1.5s
    snap_range = int(1.5 / 0.05)
    s_lo = max(0, best_idx - snap_range)
    s_hi = min(len(rms) - 1, best_idx + snap_range)
    snapped_start_hop = s_lo + int(np.argmin(rms[s_lo:s_hi + 1]))

    # Snap end boundary to a local energy trough within [eff_min, eff_max]
    nominal_end_hop = min(len(rms) - 1, snapped_start_hop + win_hops)
    min_end_hop = min(len(rms) - 1, snapped_start_hop + int(eff_min / 0.05))
    max_end_hop = min(len(rms) - 1, snapped_start_hop + int(eff_max / 0.05))
    e_lo = max(min_end_hop, nominal_end_hop - snap_range)
    e_hi = min(max_end_hop, nominal_end_hop + snap_range)
    if e_hi > e_lo:
        snapped_end_hop = e_lo + int(np.argmin(rms[e_lo:e_hi + 1]))
    else:
        snapped_end_hop = min(max_end_hop, max(min_end_hop, nominal_end_hop))

    start_sec = round(snapped_start_hop * 0.05, 2)
    end_sec = round(snapped_end_hop * 0.05, 2)
    return start_sec, end_sec


def extract_rms_envelope(wav_path: str, fps: int = 30) -> List[float]:
    """Extract normalized [0.0..1.0] per-frame RMS energy for reactive video visualizers."""
    samples, sr = _load_wav_mono(wav_path)
    hop = max(1, sr // fps)
    n_frames = len(samples) // hop
    if n_frames == 0:
        return [0.2] * (fps * 30)
    frames = samples[:n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    p95 = float(np.percentile(rms, 95)) or 1.0
    norm = np.clip(rms / p95, 0.05, 1.0)
    # Smooth slightly for fluid animation
    smoothed = np.convolve(norm, np.ones(3) / 3.0, mode="same")
    return [round(float(x), 3) for x in smoothed]


def extract_audio_features(wav_path: str, fps: int = 30, n_bands: int = 24) -> Dict[str, Any]:
    """
    Extract rich per-frame audio features for beat-reactive visualizers:
      - rms:   [0..1] overall loudness envelope
      - bass:  [0..1] sub-bass & kick energy (40-170 Hz)
      - onset: [0..1] beat/kick transient impulse with smooth decay
      - bands: N_frames x n_bands log-spaced FFT magnitudes [0..1]
    Runs deterministically on CPU with NumPy in ~0.1-0.2s per 40s clip.
    """
    samples, sr = _load_wav_mono(wav_path)
    hop = max(1, sr // fps)
    n_frames = len(samples) // hop
    if n_frames == 0:
        fallback_len = fps * 30
        return {
            "fps": fps,
            "n_bands": n_bands,
            "rms": [0.2] * fallback_len,
            "bass": [0.2] * fallback_len,
            "onset": [0.0] * fallback_len,
            "bands": [[0.2] * n_bands for _ in range(fallback_len)],
        }

    # 1. RMS envelope
    frames_raw = samples[:n_frames * hop].reshape(n_frames, hop)
    rms_raw = np.sqrt(np.mean(frames_raw ** 2, axis=1) + 1e-12)
    rms_p95 = float(np.percentile(rms_raw, 95)) or 1.0
    rms_norm = np.clip(rms_raw / rms_p95, 0.04, 1.0)
    rms_smooth = np.convolve(rms_norm, np.ones(3) / 3.0, mode="same")

    # 2. Short-time FFT per frame (Hann-windowed, 2048 samples)
    fft_size = 2048
    window = np.hanning(fft_size).astype(np.float32)
    freqs = np.fft.rfftfreq(fft_size, d=1.0 / sr)

    # Precompute log-spaced frequency bin edges from 55 Hz to min(13500 Hz, sr/2 - 100)
    f_min = 55.0
    f_max = min(13500.0, sr * 0.48)
    edges = np.geomspace(f_min, f_max, n_bands + 1)
    band_slices = []
    for b in range(n_bands):
        lo = int(np.searchsorted(freqs, edges[b]))
        hi = int(np.searchsorted(freqs, edges[b + 1]))
        if hi <= lo:
            hi = min(len(freqs), lo + 1)
        band_slices.append((lo, hi))

    bass_lo = int(np.searchsorted(freqs, 40.0))
    bass_hi = max(bass_lo + 1, int(np.searchsorted(freqs, 170.0)))

    padded = np.pad(samples, (fft_size // 2, fft_size // 2), mode="constant")
    bands_mat = np.zeros((n_frames, n_bands), dtype=np.float32)
    bass_arr = np.zeros(n_frames, dtype=np.float32)

    for i in range(n_frames):
        center = i * hop + hop // 2
        seg = padded[center:center + fft_size]
        if len(seg) < fft_size:
            seg = np.pad(seg, (0, fft_size - len(seg)))
        mag = np.abs(np.fft.rfft(seg * window))
        bass_arr[i] = float(np.mean(mag[bass_lo:bass_hi]))
        for b, (lo, hi) in enumerate(band_slices):
            bands_mat[i, b] = float(np.mean(mag[lo:hi]))

    # Log compression so highs & mids look balanced alongside bass
    bands_log = np.log1p(bands_mat * 6.0)
    col_p95 = np.percentile(bands_log, 96, axis=0)
    col_p95 = np.where(col_p95 < 1e-5, 1.0, col_p95)
    bands_norm = np.clip(bands_log / col_p95, 0.0, 1.0)

    # Asymmetric smoothing: snappy attack, fluid decay
    bands_out = np.zeros_like(bands_norm)
    prev = np.zeros(n_bands, dtype=np.float32)
    for i in range(n_frames):
        cur = bands_norm[i]
        prev = np.where(cur > prev, prev * 0.25 + cur * 0.75, prev * 0.68 + cur * 0.32)
        bands_out[i] = prev

    # Bass normalization & beat onset impulse
    bass_log = np.log1p(bass_arr * 6.0)
    bass_p95 = float(np.percentile(bass_log, 95)) or 1.0
    bass_norm = np.clip(bass_log / bass_p95, 0.0, 1.0)

    bass_diff = np.maximum(0.0, np.diff(bass_norm, prepend=bass_norm[0]))
    onset_p95 = float(np.percentile(bass_diff[bass_diff > 0], 92)) if np.any(bass_diff > 0) else 1.0
    onset_raw = np.clip(bass_diff / max(onset_p95, 1e-4), 0.0, 1.0)
    onset_raw = np.where(onset_raw < 0.25, 0.0, onset_raw)

    onset_out = np.zeros(n_frames, dtype=np.float32)
    o_prev = 0.0
    for i in range(n_frames):
        v = float(onset_raw[i])
        o_prev = max(v, o_prev * 0.76)
        onset_out[i] = o_prev

    return {
        "fps": fps,
        "n_bands": n_bands,
        "rms": [round(float(x), 3) for x in rms_smooth],
        "bass": [round(float(x), 3) for x in bass_norm],
        "onset": [round(float(x), 3) for x in onset_out],
        "bands": [[round(float(v), 3) for v in row] for row in bands_out],
    }


def prepare_track_audio(artist: str, title: str, cache_dir: Optional[str] = None) -> Dict[str, object]:
    """
    Source, clip, and normalize a 35-45s audio hook (strictly >=30s and <58s for TikTok's 60s rule).
    Prioritizes full-track yt-dlp sourcing; falls back to Deezer/iTunes 30s previews (padded to >=30s if needed).
    Returns dict with: audio_path, source_url, duration, rms_envelope, audio_features.
    """
    target_dir = cache_dir or CACHE_DIR
    os.makedirs(target_dir, exist_ok=True)
    slug = _slugify(f"{artist}_{title}")
    final_wav = os.path.join(target_dir, f"{slug}_clip.wav")

    # Reuse cached clip only if it meets the >=30s and <58s duration bounds
    if os.path.exists(final_wav) and os.path.getsize(final_wav) > 50000:
        samples, sr = _load_wav_mono(final_wav)
        cached_dur = round(len(samples) / sr, 2)
        if MIN_CLIP_DURATION <= cached_dur <= MAX_CLIP_DURATION + 3.0:
            features = extract_audio_features(final_wav, fps=30)
            return {
                "audio_path": final_wav,
                "source_url": "cached",
                "duration": cached_dur,
                "rms_envelope": features["rms"],
                "audio_features": features,
            }
        logger.info(f"Cached clip for {artist} - {title} is {cached_dur}s (< {MIN_CLIP_DURATION}s); re-sourcing.")

    raw_download = os.path.join(target_dir, f"{slug}_raw.dat")
    raw_wav = os.path.join(target_dir, f"{slug}_raw.wav")
    yt_base = os.path.join(target_dir, f"{slug}_yt")
    yt_out = None
    source_url = None

    # 1. Prioritize yt-dlp full track so we can clip a clean 35-45s hook
    yt_out = fetch_youtube_audio(artist, title, yt_base)
    if yt_out:
        source_url = "youtube_full"
        subprocess.run([
            "ffmpeg", "-y", "-i", yt_out,
            "-ar", "44100", "-ac", "2", raw_wav
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        # 2. Fallback to Deezer / iTunes 30s studio previews
        source_url = fetch_deezer_preview(artist, title) or fetch_itunes_preview(artist, title)
        if not source_url:
            raise RuntimeError(f"Could not source audio for {artist} - {title}")
        resp = requests.get(source_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        with open(raw_download, "wb") as f:
            f.write(resp.content)
        subprocess.run([
            "ffmpeg", "-y", "-i", raw_download,
            "-ar", "44100", "-ac", "2", raw_wav
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    start_sec, end_sec = find_intelligent_clip_window(raw_wav, target_duration=TARGET_CLIP_DURATION)
    raw_clip_dur = max(1.0, min(MAX_CLIP_DURATION, end_sec - start_sec))
    fade_out_start = max(0.0, raw_clip_dur - 1.2)

    # Guarantee final clip duration is strictly >= 30.0s (e.g. pad slightly if a preview is 29.7s)
    af_filters = [
        "afade=t=in:st=0:d=0.4",
        f"afade=t=out:st={fade_out_start:.2f}:d=1.2",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
    ]
    if raw_clip_dur < MIN_CLIP_DURATION:
        pad_dur = MIN_CLIP_DURATION + 0.5
        af_filters.append(f"apad=whole_dur={pad_dur:.2f}")
        out_dur = pad_dur
    else:
        out_dur = raw_clip_dur

    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-t", f"{raw_clip_dur:.2f}",
        "-i", raw_wav,
        "-af", ",".join(af_filters),
        "-t", f"{out_dur:.2f}",
        "-ar", "44100", "-ac", "2",
        final_wav
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Cleanup raw temp files
    for p in (raw_download, raw_wav, yt_out):
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    samples, sr = _load_wav_mono(final_wav)
    features = extract_audio_features(final_wav, fps=30)
    return {
        "audio_path": final_wav,
        "source_url": source_url,
        "duration": round(len(samples) / sr, 2),
        "rms_envelope": features["rms"],
        "audio_features": features,
    }
