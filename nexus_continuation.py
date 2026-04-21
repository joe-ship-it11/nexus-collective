"""
Nexus continuation — short per-channel "still in the conversation" window.

After Nexus posts a reply, we briefly treat that channel as "Nexus is actively
talking here" so the next message in that channel can be addressed to Nexus
without an @-mention or the literal "nexus" keyword. The orchestrator calls
is_in_window() in on_message as an additional trigger condition.

Pure logic — no discord.py calls, no HTTP, no persistence. State is an
in-memory dict guarded by a threading.Lock and resets on restart (window is
short, so persistence isn't worth the complexity).

Public API:
  install(bot)              — log install line, reset state
  mark_replied(channel_id)  — call after Nexus sends a chat reply
  is_in_window(channel_id)  — True if Nexus replied within window_s
  clear(channel_id)         — drop a channel's window (silence / topic shift)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DEFAULT_WINDOW_S: int = 60


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)


def _log(msg: str) -> None:
    # Match sibling modules: print-style lowercase line with module prefix.
    # Also emit through the stdlib logger so log handlers see it.
    line = f"[nexus_continuation] {msg}"
    print(line, flush=True)
    try:
        log.info(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock: threading.Lock = threading.Lock()
_last_replied: dict[int, float] = {}
_installed: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def install(bot: Any) -> None:
    """Idempotent install. Logs a single line, clears in-memory state."""
    global _installed
    try:
        if _installed:
            return
        with _lock:
            _last_replied.clear()
        _installed = True
        _log(f"installed - window={DEFAULT_WINDOW_S}s")
    except Exception as e:
        _log(f"install error ({type(e).__name__}): {e}")


def mark_replied(channel_id: int) -> None:
    """Start/refresh the continuation window for this channel."""
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return
    now = time.monotonic()
    with _lock:
        _last_replied[cid] = now


def is_in_window(channel_id: int, window_s: int = DEFAULT_WINDOW_S) -> bool:
    """True if Nexus replied in this channel within the last window_s seconds."""
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return False
    try:
        w = float(window_s)
    except (TypeError, ValueError):
        w = float(DEFAULT_WINDOW_S)
    if w <= 0:
        return False
    now = time.monotonic()
    with _lock:
        ts = _last_replied.get(cid)
        if ts is None:
            return False
        if (now - ts) <= w:
            return True
        # Expired — drop it so the dict doesn't grow forever.
        _last_replied.pop(cid, None)
        return False


def clear(channel_id: int) -> None:
    """Drop a channel's window (e.g. long silence, detected topic shift)."""
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return
    with _lock:
        _last_replied.pop(cid, None)


__all__ = [
    "install",
    "mark_replied",
    "is_in_window",
    "clear",
    "DEFAULT_WINDOW_S",
]
