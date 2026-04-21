"""
Nexus continuation — scoped per-channel "still in the conversation" window.

After Nexus replies to a specific user, we briefly treat THAT user's next
message in THAT channel as addressed to Nexus (no @-mention needed). Messages
from OTHER users in the same channel during the window do NOT trigger
continuation — they need to @ or name-trigger Nexus explicitly.

This fixes the vending-machine feel where Nexus would butt into a conversation
between two other humans just because it had recently posted. Concrete pattern
we saw in the wild: Nexus replies to user A, then users B and C start talking
to each other in the same channel, and Nexus would reply to every one of their
messages because the window was channel-scoped, not user-scoped.

Pure logic — no discord.py calls, no HTTP, no persistence. State is an
in-memory dict guarded by a threading.Lock and resets on restart (window is
short, so persistence isn't worth the complexity).

Public API:
  install(bot)
  mark_replied(channel_id, user_id=None)  — Nexus just replied; record recipient.
                                             user_id=None means "no specific
                                             user" (e.g. proactive channel
                                             chime), in which case the window
                                             won't match anyone.
  is_in_window(channel_id, user_id)       — True iff Nexus replied to THIS
                                             user in THIS channel within the
                                             window. Scoped match.
  clear(channel_id)                        — drop a channel's window.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Shorter than the old 60s default — 30s reads as "we're in the same breath"
# rather than "the bot is still clinging to a minute-old exchange."
DEFAULT_WINDOW_S: int = 30


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)


def _log(msg: str) -> None:
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
# channel_id -> (user_id, monotonic_ts). user_id = 0 means "ambient" (no
# specific recipient); such windows don't match any user's next message.
_last_replied: dict[int, tuple[int, float]] = {}
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
        _log(f"installed — window={DEFAULT_WINDOW_S}s, user-scoped")
    except Exception as e:
        _log(f"install error ({type(e).__name__}): {e}")


def mark_replied(channel_id: int, user_id: Optional[int] = None) -> None:
    """Start/refresh the continuation window for this channel.

    user_id: the user Nexus just replied TO. When their next message arrives
    in this channel within DEFAULT_WINDOW_S, continuation fires for them and
    only them. Pass None for ambient broadcasts (proactive chimes with no
    specific recipient) — such windows won't match any user, effectively a
    no-op for continuation but still useful for other bookkeeping.
    """
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return
    try:
        uid = int(user_id) if user_id is not None else 0
    except (TypeError, ValueError):
        uid = 0
    now = time.monotonic()
    with _lock:
        _last_replied[cid] = (uid, now)


def is_in_window(channel_id: int, user_id: int,
                 window_s: int = DEFAULT_WINDOW_S) -> bool:
    """True iff Nexus replied to THIS user in THIS channel within window_s.

    Scoped match: the stored recipient user_id must equal the passed user_id.
    If Nexus's last reply was to someone else in this channel, this returns
    False — even though the window is still open. That's the whole point.
    """
    try:
        cid = int(channel_id)
        uid = int(user_id)
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
        entry = _last_replied.get(cid)
        if entry is None:
            return False
        stored_uid, ts = entry
        if (now - ts) > w:
            # Expired — drop so the dict doesn't grow forever.
            _last_replied.pop(cid, None)
            return False
        return stored_uid == uid


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
