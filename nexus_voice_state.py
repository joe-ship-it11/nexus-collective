"""
Tiny persistence for voice state — so Nexus can auto-rejoin voice after
a gateway disconnect or a full restart.

Stores: { "guild_id": str, "channel_id": str, "joined_at": iso_ts }
File:   voice_state.json (next to nexus_bot.py)

Used by nexus_bot.on_ready + on_resumed to rejoin + re-start listening.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

_STATE_FILE = Path(__file__).parent / "voice_state.json"


def remember(guild_id: int | str, channel_id: int | str) -> None:
    """Record that Nexus is currently in <channel_id>."""
    try:
        _STATE_FILE.write_text(
            json.dumps({
                "guild_id": str(guild_id),
                "channel_id": str(channel_id),
                "joined_at": dt.datetime.now().isoformat(timespec="seconds"),
            }),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[voice_state.remember] error: {type(e).__name__}: {e}")


def forget() -> None:
    """Clear the voice state — called on /leave or clean shutdown."""
    try:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except Exception as e:
        print(f"[voice_state.forget] error: {type(e).__name__}: {e}")


def get() -> Optional[dict]:
    """Return last voice state or None."""
    try:
        if not _STATE_FILE.exists():
            return None
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[voice_state.get] error: {type(e).__name__}: {e}")
        return None
