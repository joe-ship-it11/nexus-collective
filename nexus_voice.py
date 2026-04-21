"""
Nexus voice: TTS via edge-tts (free, no API key, neural voices) +
Discord VC playback helpers.

Phase 2.5: speak-only (no listen yet).
"""

import asyncio
import os
import uuid
from pathlib import Path

import edge_tts

import config


# Microsoft neural voices. Nexus's default is deep/casual/modern.
# Swap in config.py if you want something different — a few good options:
#   en-US-AndrewMultilingualNeural   (deep, casual, modern)  ← default
#   en-US-BrianMultilingualNeural    (warm, grounded)
#   en-US-SteffanNeural              (sharp, clipped)
#   en-US-GuyNeural                  (neutral, clean)
#   en-US-AriaNeural                 (smooth, female)
#   en-US-AvaMultilingualNeural      (sharp, female)
DEFAULT_VOICE = getattr(config, "NEXUS_TTS_VOICE", "en-US-AndrewMultilingualNeural")

TEMP_DIR = Path(__file__).parent / "tts_temp"
TEMP_DIR.mkdir(exist_ok=True)


async def synthesize(text: str, voice: str = None) -> Path:
    """
    Generate a TTS mp3 for `text`. Returns path to the file.
    Caller is responsible for cleanup (use `cleanup_callback`).
    """
    voice = voice or DEFAULT_VOICE
    out = TEMP_DIR / f"tts_{uuid.uuid4().hex}.mp3"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out))
    return out


def cleanup_callback(path: Path):
    """Return a discord VoiceClient `after=` callback that deletes the file."""
    def _after(err):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
    return _after


def clear_temp():
    """Housekeeping — nuke leftover TTS files."""
    for p in TEMP_DIR.glob("*.mp3"):
        try:
            p.unlink()
        except Exception:
            pass
