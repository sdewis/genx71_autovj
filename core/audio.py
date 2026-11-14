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
from typing import Optional, Tuple, List, Dict
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


def find_intelligent_clip_window(wav_path: str, target_duration: float = 29.5) -> Tuple[float, float]:
    """
    Analyze audio energy & phrase pauses to find the best ~30s window.
    - If source is already <= 32s (e.g. Deezer/iTunes 30s preview), snaps start/end to
      silence/breath gaps near the edges so it doesn't cut mid-beat.
    - If source is a full track (> 45s), locates the highest-energy chorus/hook region
      and snaps the start to a phrase onset and the end to a lyric pause/trough.
    """
    samples, sr = _load_wav_mono(wav_path)
    total_dur = len(samples) / sr

    if total_dur <= target_duration + 1.5:
        return 0.0, min(total_dur, target_duration)

    # Compute 50ms RMS energy envelope
    hop = int(sr * 0.05)
    n_hops = len(samples) // hop
    frames = samples[:n_hops * hop].reshape(n_hops, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-9)

    win_hops = int(target_duration / 0.05)
    if win_hops >= len(rms):
        return 0.0, total_dur

    # Sliding window sum to find peak energy chorus/verse section
    # Avoid very first 10 seconds (often sparse intro) if track > 90s
    start_search_hop = int(10.0 / 0.05) if total_dur > 90.0 else 0
    end_search_hop = len(rms) - win_hops

    if end_search_hop <= start_search_hop:
        return 0.0, min(total_dur, target_duration)

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

    # Snap end boundary to a local energy trough (end of lyric line) within +/- 1.5s around target
    nominal_end_hop = min(len(rms) - 1, snapped_start_hop + win_hops)
    e_lo = max(snapped_start_hop + int(20.0 / 0.05), nominal_end_hop - snap_range)
    e_hi = min(len(rms) - 1, nominal_end_hop + snap_range)
    if e_hi > e_lo:
        snapped_end_hop = e_lo + int(np.argmin(rms[e_lo:e_hi + 1]))
    else:
        snapped_end_hop = nominal_end_hop

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


def prepare_track_audio(artist: str, title: str, cache_dir: Optional[str] = None) -> Dict[str, object]:
    """
    Source, clip, and normalize a ~30s audio hook for the given track.
    Returns dict with: audio_path, source_url, duration, rms_envelope.
    """
    target_dir = cache_dir or CACHE_DIR
    os.makedirs(target_dir, exist_ok=True)
    slug = _slugify(f"{artist}_{title}")
    final_wav = os.path.join(target_dir, f"{slug}_clip.wav")

    if os.path.exists(final_wav) and os.path.getsize(final_wav) > 50000:
        samples, sr = _load_wav_mono(final_wav)
        return {
            "audio_path": final_wav,
            "source_url": "cached",
            "duration": round(len(samples) / sr, 2),
            "rms_envelope": extract_rms_envelope(final_wav, fps=30)
        }

    source_url = fetch_deezer_preview(artist, title) or fetch_itunes_preview(artist, title)
    raw_download = os.path.join(target_dir, f"{slug}_raw.dat")
    raw_wav = os.path.join(target_dir, f"{slug}_raw.wav")

    if source_url:
        resp = requests.get(source_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        with open(raw_download, "wb") as f:
            f.write(resp.content)
        subprocess.run([
            "ffmpeg", "-y", "-i", raw_download,
            "-ar", "44100", "-ac", "2", raw_wav
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        source_url = "youtube_fallback"
        yt_out = fetch_youtube_audio(artist, title, os.path.join(target_dir, f"{slug}_yt"))
        if not yt_out:
            raise RuntimeError(f"Could not source audio for {artist} - {title}")
        subprocess.run([
            "ffmpeg", "-y", "-i", yt_out,
            "-ar", "44100", "-ac", "2", raw_wav
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    start_sec, end_sec = find_intelligent_clip_window(raw_wav, target_duration=29.5)
    clip_dur = max(5.0, end_sec - start_sec)
    fade_out_start = max(0.0, clip_dur - 1.2)

    # Clip with loudnorm + smooth fade in/out
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-t", str(clip_dur),
        "-i", raw_wav,
        "-af", f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out_start:.2f}:d=1.2,loudnorm=I=-14:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "2",
        final_wav
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Cleanup raw temp files
    for p in (raw_download, raw_wav):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    samples, sr = _load_wav_mono(final_wav)
    return {
        "audio_path": final_wav,
        "source_url": source_url,
        "duration": round(len(samples) / sr, 2),
        "rms_envelope": extract_rms_envelope(final_wav, fps=30)
    }
