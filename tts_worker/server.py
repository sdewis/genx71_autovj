"""
GenX71 AutoVJ — CPU Zero-Shot Voice Cloning Sidecar (Coqui XTTS-v2).

Exposes a minimal internal HTTP API on port 8711:
  - GET  /health      : Liveness & model readiness check
  - POST /synthesize  : Synthesizes a spoken DJ hook WAV cloned from 1+ reference WAVs

Runs strictly sequentially (1 synthesis at a time via lock) so CPU cores are never
oversubscribed on a local Kubuntu PC / Docker host.
"""

import os
import time
import wave
import logging
import threading
import subprocess
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("genx71.tts_worker")

MODEL_NAME = os.environ.get("XTTS_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2")
os.environ.setdefault("COQUI_TOS_AGREED", "1")

app = FastAPI(title="GenX71 TTS Worker", version="1.0.0")

_synth_lock = threading.Lock()
_tts_model = None
_model_error: Optional[str] = None


def _get_tts_model():
    global _tts_model, _model_error
    if _tts_model is not None:
        return _tts_model
    try:
        logger.info("Loading XTTS-v2 model on CPU (%s)...", MODEL_NAME)
        t0 = time.monotonic()
        from TTS.api import TTS

        _tts_model = TTS(MODEL_NAME, gpu=False)
        _model_error = None
        logger.info("XTTS-v2 model loaded in %.1fs", time.monotonic() - t0)
        return _tts_model
    except Exception as exc:
        _model_error = str(exc)
        logger.exception("Failed to load XTTS-v2 model")
        raise


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate() or 22050
        return round(wf.getnframes() / float(sr), 2)


def _postprocess_wav(raw_wav: str, out_wav: str) -> float:
    """Trim dead silence at start/end, resample to 44.1kHz mono, and normalize loudness."""
    af = (
        "highpass=f=75,"
        "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-45dB,"
        "areverse,"
        "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-45dB,"
        "areverse,"
        "loudnorm=I=-13:TP=-1.5:LRA=7"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", raw_wav,
            "-af", af,
            "-ar", "44100",
            "-ac", "1",
            "-sample_fmt", "s16",
            out_wav,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _wav_duration(out_wav)


class SynthesizeRequest(BaseModel):
    text: str
    speaker_wavs: List[str]
    output_path: str
    language: str = "en"
    speed: float = 1.05
    temperature: float = 0.58
    top_p: float = 0.78
    top_k: int = 40
    repetition_penalty: float = 5.0
    gpt_cond_len: int = 30
    gpt_cond_chunk_len: int = 6
    max_ref_length: int = 30


_latents_cache: dict = {}


def _get_xtts_latents(xtts_model, valid_wavs: List[str], max_ref_length: int, gpt_cond_len: int, gpt_cond_chunk_len: int):
    """Compute and cache XTTS-v2 speaker conditioning latents keyed by file paths + mtimes."""
    key_files = tuple((p, os.path.getmtime(p), os.path.getsize(p)) for p in valid_wavs)
    cache_key = (key_files, int(max_ref_length), int(gpt_cond_len), int(gpt_cond_chunk_len))
    cached = _latents_cache.get(cache_key)
    if cached is not None:
        return cached

    latents = xtts_model.get_conditioning_latents(
        audio_path=valid_wavs if len(valid_wavs) > 1 else valid_wavs[0],
        max_ref_length=int(max_ref_length),
        gpt_cond_len=int(gpt_cond_len),
        gpt_cond_chunk_len=int(gpt_cond_chunk_len),
        sound_norm_refs=True,
    )
    # Keep cache bounded (UK + US profiles)
    if len(_latents_cache) >= 8:
        _latents_cache.clear()
    _latents_cache[cache_key] = latents
    return latents


def _write_pcm16_wav(path: str, wav_samples, sample_rate: int = 24000) -> None:
    import numpy as np

    arr = np.asarray(wav_samples, dtype=np.float32).squeeze()
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "xtts_v2",
        "model_name": MODEL_NAME,
        "model_loaded": _tts_model is not None,
        "device": "cpu",
        "busy": _synth_lock.locked(),
        "error": _model_error,
    }


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text must not be empty.")

    valid_wavs = [p for p in req.speaker_wavs if p and os.path.exists(p)]
    if not valid_wavs:
        raise HTTPException(
            status_code=400,
            detail=f"No valid speaker reference WAV files found: {req.speaker_wavs}",
        )

    out_dir = os.path.dirname(os.path.abspath(req.output_path))
    os.makedirs(out_dir, exist_ok=True)
    raw_tmp = req.output_path + ".raw.wav"

    t0 = time.monotonic()
    with _synth_lock:
        try:
            tts = _get_tts_model()
            logger.info(
                "Synthesizing %d chars with %d reference clip(s) (temp=%.2f, cond=%ds) -> %s",
                len(text), len(valid_wavs), req.temperature, req.gpt_cond_len, req.output_path,
            )

            xtts_model = getattr(getattr(tts, "synthesizer", None), "tts_model", None)
            if xtts_model is not None and hasattr(xtts_model, "get_conditioning_latents") and hasattr(xtts_model, "inference"):
                gpt_cond_latent, speaker_embedding = _get_xtts_latents(
                    xtts_model,
                    valid_wavs,
                    max_ref_length=req.max_ref_length,
                    gpt_cond_len=req.gpt_cond_len,
                    gpt_cond_chunk_len=req.gpt_cond_chunk_len,
                )
                out = xtts_model.inference(
                    text=text,
                    language=req.language or "en",
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=float(req.temperature),
                    top_p=float(req.top_p),
                    top_k=int(req.top_k),
                    repetition_penalty=float(req.repetition_penalty),
                    speed=float(req.speed),
                    enable_text_splitting=True,
                )
                sr = getattr(getattr(xtts_model, "config", None), "audio", None)
                out_sr = getattr(sr, "output_sample_rate", 24000) if sr is not None else 24000
                _write_pcm16_wav(raw_tmp, out["wav"], sample_rate=out_sr)
            else:
                speaker_arg = valid_wavs if len(valid_wavs) > 1 else valid_wavs[0]
                kwargs = {
                    "text": text,
                    "speaker_wav": speaker_arg,
                    "language": req.language or "en",
                    "file_path": raw_tmp,
                    "split_sentences": True,
                }
                try:
                    tts.tts_to_file(**kwargs, speed=float(req.speed))
                except TypeError:
                    tts.tts_to_file(**kwargs)

            duration = _postprocess_wav(raw_tmp, req.output_path)
            elapsed = round(time.monotonic() - t0, 2)
            logger.info("Synthesized %.2fs clip in %.1fs -> %s", duration, elapsed, req.output_path)
            return {
                "ok": True,
                "output_path": req.output_path,
                "duration": duration,
                "elapsed_sec": elapsed,
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("TTS synthesis failed")
            raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {exc}")
        finally:
            if os.path.exists(raw_tmp):
                try:
                    os.remove(raw_tmp)
                except OSError:
                    pass
