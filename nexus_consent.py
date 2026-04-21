"""
Consent + privacy state for Nexus.

Tracks:
  - Opt-out status per user (stop recording their messages/voice entirely)
  - Temporary mute windows (pause voice transcription for N minutes)
  - Server-wide proactivity mute (quiet_until)
  - Per-user shy flag (no unprompted chimes at that user)

Backed by a single JSON file: consent.json. Atomic write via temp+rename.
Thread-safe via a module-level lock.

Public API:
    is_opted_out(user_id) -> bool
    set_opted_out(user_id, value=True) -> None
    is_muted_now(user_id=None) -> bool           # user_id=None → global mute
    set_mute(user_id, until_ts: float) -> None   # user_id=None → global
    mute_for_minutes(user_id, minutes: float) -> float  # returns unix ts when lifts
    is_quiet() -> bool                            # server-wide proactivity mute
    get_quiet_until() -> float
    set_quiet_until(until_ts: float) -> None
    quiet_for_minutes(minutes: float) -> float
    clear_quiet() -> None
    is_shy(user_id) -> bool
    set_shy(user_id, value=True) -> None
    dump() -> dict                                # debug
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

import config

_PATH: Path = config.ROOT / "consent.json"
_lock = threading.Lock()
_cache: Optional[dict] = None


def _default() -> dict:
    return {
        "opted_out": {},
        "mutes": {},
        "quiet_until": 0.0,
        "shy": {},
    }


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        if _PATH.exists():
            _cache = json.loads(_PATH.read_text(encoding="utf-8"))
            # Harden shape — missing keys on older files shouldn't crash
            _cache.setdefault("opted_out", {})
            _cache.setdefault("mutes", {})
            _cache.setdefault("quiet_until", 0.0)
            _cache.setdefault("shy", {})
        else:
            _cache = _default()
    except Exception as e:
        print(f"[nexus_consent] load error, starting empty: {e}")
        _cache = _default()
    return _cache


def _save() -> None:
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------
def is_opted_out(user_id) -> bool:
    with _lock:
        data = _load()
        return bool(data["opted_out"].get(str(user_id), False))


def set_opted_out(user_id, value: bool = True) -> None:
    with _lock:
        data = _load()
        uid = str(user_id)
        if value:
            data["opted_out"][uid] = True
        else:
            data["opted_out"].pop(uid, None)
        _save()


# ---------------------------------------------------------------------------
# Mutes — temporary pauses
# ---------------------------------------------------------------------------
def is_muted_now(user_id=None) -> bool:
    with _lock:
        data = _load()
        key = str(user_id) if user_id is not None else "__global__"
        until = float(data["mutes"].get(key, 0) or 0)
        return until > time.time()


def set_mute(user_id, until_ts: float) -> None:
    with _lock:
        data = _load()
        key = str(user_id) if user_id is not None else "__global__"
        if until_ts <= time.time():
            data["mutes"].pop(key, None)
        else:
            data["mutes"][key] = float(until_ts)
        _save()


def mute_for_minutes(user_id, minutes: float) -> float:
    """Mute for N minutes from now. Returns the unix timestamp when it lifts."""
    until = time.time() + float(minutes) * 60.0
    set_mute(user_id, until)
    return until


def clear_mute(user_id=None) -> None:
    set_mute(user_id, 0.0)


# ---------------------------------------------------------------------------
# Quiet — server-wide proactivity mute
# ---------------------------------------------------------------------------
def is_quiet() -> bool:
    """Server-wide proactivity mute. True if quiet_until > now()."""
    with _lock:
        data = _load()
        return float(data.get("quiet_until", 0) or 0) > time.time()


def get_quiet_until() -> float:
    """Returns the unix ts when quiet lifts. 0 if not currently quiet."""
    with _lock:
        data = _load()
        until = float(data.get("quiet_until", 0) or 0)
        return until if until > time.time() else 0.0


def set_quiet_until(until_ts: float) -> None:
    """Set the unix ts when proactivity mute lifts. <=now() clears it."""
    with _lock:
        data = _load()
        if until_ts <= time.time():
            data["quiet_until"] = 0.0
        else:
            data["quiet_until"] = float(until_ts)
        _save()


def quiet_for_minutes(minutes: float) -> float:
    """Mute proactivity for N minutes from now. Returns lift-ts."""
    until = time.time() + float(minutes) * 60.0
    set_quiet_until(until)
    return until


def clear_quiet() -> None:
    """Lift proactivity mute immediately."""
    set_quiet_until(0.0)


# ---------------------------------------------------------------------------
# Shy — per-user opt-out from unprompted chimes
# ---------------------------------------------------------------------------
def is_shy(user_id) -> bool:
    """True if this user has opted out of unprompted chimes at them."""
    with _lock:
        data = _load()
        return bool(data.get("shy", {}).get(str(user_id), False))


def set_shy(user_id, value: bool = True) -> None:
    """Toggle per-user shy flag."""
    with _lock:
        data = _load()
        data.setdefault("shy", {})
        uid = str(user_id)
        if value:
            data["shy"][uid] = True
        else:
            data["shy"].pop(uid, None)
        _save()


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def dump() -> dict:
    with _lock:
        return dict(_load())
