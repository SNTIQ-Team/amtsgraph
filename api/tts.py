"""Text-to-speech — stateless espeak-ng synthesis (opus when available).

Server-side fallback for the platform's read-aloud feature: browsers whose
Web Speech API reports no local voices (typically Linux Chrome without
speech-dispatcher voices) POST the page text here and play the returned audio.

Contract: strict language allow-list, hard text cap, text handed to the
engine on stdin (never argv), synth -> stream -> discard. Nothing about the
text content is ever logged. A small in-memory LRU keyed by a hash of
(lang, text) absorbs repeat requests for the same page so the 1-GB host is
not re-synthesizing identical audio; entries are bytes + hash only.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from collections import OrderedDict
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/tts", tags=["tts"])

# Keep well below anything that could tie up the 1-GB host: ~4000 chars is
# a few minutes of speech and synthesizes in well under a second.
MAX_CHARS = 4_000
_TIMEOUT_S = 10

# opusenc (opus-tools, ~165 KiB) shrinks the WAV ~15x; fall back to raw WAV
# when it is not installed. Resolved once at import.
_OPUSENC = shutil.which("opusenc")

# in-memory LRU: repeat reads of the same page skip synthesis entirely
_CACHE_MAX_ENTRIES = 24
_CACHE_MAX_BYTES = 16 * 1024 * 1024
_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _cache_get(key: str) -> tuple[bytes, str] | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
        return hit


def _cache_put(key: str, audio: bytes, media_type: str) -> None:
    global _cache_bytes
    if len(audio) > _CACHE_MAX_BYTES:
        return
    with _cache_lock:
        if key in _cache:
            return
        _cache[key] = (audio, media_type)
        _cache_bytes += len(audio)
        while len(_cache) > _CACHE_MAX_ENTRIES or _cache_bytes > _CACHE_MAX_BYTES:
            _, (evicted, _mt) = _cache.popitem(last=False)
            _cache_bytes -= len(evicted)

# espeak-ng voice ids happen to equal the ISO 639-1 codes for all four
# platform languages (Ukrainian is "uk", not the app-internal "ua").
Lang = Literal["de", "en", "ru", "uk"]


class SpeakPayload(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CHARS)
    lang: Lang

    @field_validator("text", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        # Strip BEFORE the length constraints so padding cannot dodge the
        # cap and whitespace-only input fails min_length.
        return v.strip() if isinstance(v, str) else v


@router.post("/speak")
def tts_speak(payload: SpeakPayload) -> Response:
    key = hashlib.sha256(
        f"{payload.lang}\0{payload.text}".encode("utf-8")).hexdigest()
    hit = _cache_get(key)
    if hit is not None:
        audio, media_type = hit
        return Response(content=audio, media_type=media_type,
                        headers={"Cache-Control": "no-store",
                                 "X-TTS-Cache": "hit"})

    # argv carries only validated flags; the text travels on stdin, so no
    # user input ever reaches the argument vector.
    cmd = [
        "espeak-ng", "--stdout", "--stdin",
        "-v", payload.lang, "-s", "155", "-p", "45",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=payload.text.encode("utf-8"),
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise HTTPException(503, "tts engine unavailable")
    except (subprocess.TimeoutExpired, OSError):
        raise HTTPException(503, "tts synthesis failed")
    if proc.returncode != 0 or not proc.stdout:
        raise HTTPException(503, "tts synthesis failed")

    audio, media_type = proc.stdout, "audio/wav"
    if _OPUSENC:
        # WAV -> ogg/opus (~15x smaller); on any failure ship the WAV
        try:
            enc = subprocess.run(
                [_OPUSENC, "--quiet", "--bitrate", "24", "-", "-"],
                input=audio, capture_output=True, timeout=_TIMEOUT_S)
            if enc.returncode == 0 and enc.stdout:
                audio, media_type = enc.stdout, "audio/ogg"
        except (subprocess.TimeoutExpired, OSError):
            pass

    _cache_put(key, audio, media_type)
    return Response(
        content=audio,
        media_type=media_type,
        # page text is dynamic — the JS CacheStorage layer decides what to
        # keep on the client; HTTP caches must not
        headers={"Cache-Control": "no-store"},
    )
