"""
Nexus feedback — reaction-emoji learning.

When Nexus posts (proactive chime, follow-up, skill link, vision react,
quote, voice chime), the orchestrator calls stamp_chime(msg, kind, confidence)
to record that this message_id is a Nexus post. Later, users react with
emojis; the bot's on_raw_reaction_add handler forwards the payload to
on_reaction(payload), which checks if the reacted message is one of ours
and, if so, writes a JSON line to feedback_log.jsonl tagging the reaction
as positive / negative / neutral.

get_stats(window_h) aggregates the last N hours of posts and reactions
into a per-kind hit-ratio dict. The digest module consumes that to show
yesterday's signal; future tuning passes read it to adjust chime
thresholds.

Design notes:
  - Pure-local state. Nothing hits mem0. Two files next to the bot:
      feedback_state.json   — in-flight stamps (pruned at 7d, capped at 5000)
      feedback_log.jsonl    — append-only reaction log (unbounded)
  - State writes go through a threading.Lock + atomic tmp+replace.
  - Reaction log uses a single open-append-close per line. That's atomic
    for short writes on POSIX and Windows; we still hold a lock to avoid
    interleaved writes from two threads.
  - Every public function wraps its body in try/except so errors never
    bubble into the caller's event loop.
  - No discord API calls on reaction — we look the message up in our
    in-memory stamps dict, which is populated from state on install and
    updated on every stamp_chime.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import config

try:  # discord is always installed in this project, but import defensively
    import discord  # noqa: F401
except Exception:  # pragma: no cover
    discord = None  # type: ignore


log = logging.getLogger(__name__)


def _plog(msg: str) -> None:
    """Mirror key logging lines to stdout so they show up in bot.log
    regardless of whether the logging handler is wired to stdout."""
    print(f"[nexus_feedback] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Live-tunable emoji buckets (data, not code — these intentionally use glyphs)
# ---------------------------------------------------------------------------
POSITIVE_EMOJI: set[str] = {
    "\U0001F44D",  # thumbs up
    "\u2764\ufe0f",  # red heart
    "\U0001F602",  # face with tears of joy
    "\U0001F480",  # skull
    "\U0001F525",  # fire
    "\U0001F389",  # party popper
    "\u2728",  # sparkles
    "\U0001F64F",  # folded hands
    "\U0001F4AF",  # hundred points
    "\U0001F44F",  # clapping hands
    "\U0001F60D",  # smiling face with heart-eyes
    "\U0001F923",  # rolling on the floor laughing
}
NEGATIVE_EMOJI: set[str] = {
    "\U0001F44E",  # thumbs down
    "\U0001F644",  # face with rolling eyes
    "\U0001F910",  # zipper-mouth face
    "\U0001F62C",  # grimacing face
    "\U0001F612",  # unamused face
    "\U0001F928",  # face with raised eyebrow
    "\u274C",  # cross mark
    "\U0001F6AB",  # no entry sign
}
NEUTRAL_EMOJI: set[str] = set()


# ---------------------------------------------------------------------------
# Paths + config
# ---------------------------------------------------------------------------
STATE_PATH = Path(config.ROOT) / "feedback_state.json"
LOG_PATH = Path(config.ROOT) / "feedback_log.jsonl"

STAMP_MAX_AGE_S = 7 * 24 * 60 * 60  # 7 days
STAMP_MAX_COUNT = 5000


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_log_lock = threading.Lock()
_stamps: dict[str, dict[str, Any]] = {}  # message_id_str -> record
_installed = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _polarity(emoji_str: str) -> str:
    """Classify an emoji glyph into positive / negative / neutral."""
    if emoji_str in POSITIVE_EMOJI:
        return "positive"
    if emoji_str in NEGATIVE_EMOJI:
        return "negative"
    return "neutral"


def _emoji_from_payload(payload: Any) -> Optional[str]:
    """Extract a plain unicode-glyph string from a RawReactionActionEvent.

    Custom (guild) emoji return their :name: form so we still log them, but
    they'll classify as neutral since the sets contain only unicode glyphs.
    Returns None if the payload has no usable emoji.
    """
    try:
        emo = getattr(payload, "emoji", None)
        if emo is None:
            return None
        # discord.PartialEmoji: .id is None for unicode emojis, .name is the glyph
        emo_id = getattr(emo, "id", None)
        name = getattr(emo, "name", None)
        if emo_id is None:
            # Unicode emoji — .name is the glyph itself
            return str(name) if name else None
        # Custom guild emoji — fall back to :name:
        if name:
            return f":{name}:"
        return None
    except Exception:
        return None


def _prune_stamps(stamps: dict[str, dict[str, Any]], now_ts: float) -> dict[str, dict[str, Any]]:
    """Drop stamps older than STAMP_MAX_AGE_S, then cap at STAMP_MAX_COUNT (oldest first)."""
    cutoff = now_ts - STAMP_MAX_AGE_S
    # Parse posted_at once, keep only recent
    fresh: list[tuple[float, str, dict[str, Any]]] = []
    for mid, rec in stamps.items():
        posted_at = rec.get("posted_at")
        ts: float
        try:
            if isinstance(posted_at, (int, float)):
                ts = float(posted_at)
            else:
                # ISO string path
                ts = dt.datetime.fromisoformat(str(posted_at)).timestamp()
        except Exception:
            # Unknown shape — treat as now so we don't lose recent ones spuriously
            ts = now_ts
        if ts < cutoff:
            continue
        fresh.append((ts, mid, rec))

    # Cap — keep newest STAMP_MAX_COUNT
    if len(fresh) > STAMP_MAX_COUNT:
        fresh.sort(key=lambda t: t[0], reverse=True)
        fresh = fresh[:STAMP_MAX_COUNT]

    return {mid: rec for (_ts, mid, rec) in fresh}


# ---------------------------------------------------------------------------
# State I/O (lock held; no awaits inside)
# ---------------------------------------------------------------------------
def _load_state_from_disk() -> dict[str, dict[str, Any]]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log.warning("[nexus_feedback] state load error (%s): %s — starting fresh", type(e).__name__, e)
        return {}
    stamps = data.get("stamps") if isinstance(data, dict) else {}
    if not isinstance(stamps, dict):
        return {}
    # Keys must be strings
    return {str(k): v for k, v in stamps.items() if isinstance(v, dict)}


def _save_state_locked() -> None:
    """Must be called with _state_lock held. Prunes before writing."""
    global _stamps
    _stamps = _prune_stamps(_stamps, _now_ts())
    payload = {"stamps": _stamps}
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning("[nexus_feedback] state save error (%s): %s", type(e).__name__, e)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _append_log_line(record: dict[str, Any]) -> None:
    """Append one JSON line to feedback_log.jsonl. Lock-guarded."""
    line = json.dumps(record, ensure_ascii=False)
    with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            log.warning("[nexus_feedback] log append error (%s): %s", type(e).__name__, e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def install(bot: Any) -> None:
    """
    Orchestrator calls once at bot startup. Loads stamps from disk into
    the in-memory dict, logs an install line. `bot` is accepted for
    symmetry with sibling modules; no handlers are registered here.
    """
    global _stamps, _installed
    try:
        with _state_lock:
            loaded = _load_state_from_disk()
            _stamps = _prune_stamps(loaded, _now_ts())
            # Persist the pruned shape so next load is clean
            _save_state_locked()
            count = len(_stamps)
        _installed = True
        log.info(
            "[nexus_feedback] installed — %d stamps loaded, state=%s log=%s",
            count, STATE_PATH.name, LOG_PATH.name,
        )
    except Exception as e:
        log.warning("[nexus_feedback] install error (%s): %s", type(e).__name__, e)


def stamp_chime(
    message: Any,
    kind: str,
    confidence: float,
    scope: str = "tnc",
) -> None:
    """
    Call right after Nexus posts a message. Records message_id → metadata
    so a later reaction on that message can be attributed to this chime.
    Safe to call with None/malformed inputs — errors are swallowed.
    """
    try:
        if message is None:
            log.debug("[nexus_feedback] stamp_chime: message is None, skipping")
            return
        msg_id = getattr(message, "id", None)
        if msg_id is None:
            log.debug("[nexus_feedback] stamp_chime: no message.id, skipping")
            return

        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        channel_id = getattr(channel, "id", None)
        guild_id = getattr(guild, "id", None)

        try:
            conf_f = float(confidence)
        except (TypeError, ValueError):
            conf_f = 0.0

        record: dict[str, Any] = {
            "message_id": int(msg_id),
            "channel_id": int(channel_id) if channel_id is not None else None,
            "guild_id": int(guild_id) if guild_id is not None else None,
            "kind": str(kind or "unknown"),
            "confidence": round(conf_f, 4),
            "scope": str(scope or "tnc"),
            "posted_at": _now_iso(),
        }

        with _state_lock:
            _stamps[str(msg_id)] = record
            _save_state_locked()

        log.info(
            "[nexus_feedback] stamped msg_id=%s kind=%s conf=%.2f scope=%s",
            msg_id, record["kind"], conf_f, record["scope"],
        )
        _plog(f"stamp kind={record['kind']} conf={conf_f:.2f} msg_id={msg_id}")
    except Exception as e:
        log.warning("[nexus_feedback] stamp_chime error (%s): %s", type(e).__name__, e)


def on_reaction(payload: Any) -> None:
    """
    Call from bot's on_raw_reaction_add handler. If the reacted message
    is a stamped Nexus message, append a line to feedback_log.jsonl.
    No discord API calls — we look up message_id in our in-memory stamps.
    Silently no-ops on any error.
    """
    try:
        if payload is None:
            return
        msg_id = getattr(payload, "message_id", None)
        if msg_id is None:
            return

        key = str(msg_id)
        # Snapshot of the stamp under lock, release before disk I/O
        with _state_lock:
            stamp = _stamps.get(key)
        if stamp is None:
            # Not one of ours — silent no-op
            return

        emoji_str = _emoji_from_payload(payload)
        if not emoji_str:
            log.debug("[nexus_feedback] on_reaction: no emoji in payload for msg_id=%s", msg_id)
            return

        polarity = _polarity(emoji_str)
        user_id = getattr(payload, "user_id", None)

        record = {
            "ts": _now_iso(),
            "msg_id": int(msg_id),
            "kind": stamp.get("kind"),
            "confidence": stamp.get("confidence"),
            "scope": stamp.get("scope"),
            "user_id": int(user_id) if user_id is not None else None,
            "emoji": emoji_str,
            "polarity": polarity,
        }
        _append_log_line(record)

        log.info(
            "[nexus_feedback] reaction logged msg_id=%s kind=%s emoji=%s polarity=%s user=%s",
            msg_id, stamp.get("kind"), emoji_str, polarity, user_id,
        )
        _plog(
            f"reaction kind={stamp.get('kind')} emoji={emoji_str} "
            f"polarity={polarity} msg_id={msg_id} user={user_id}"
        )
    except Exception as e:
        log.warning("[nexus_feedback] on_reaction error (%s): %s", type(e).__name__, e)


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------
def _iter_log_records(since_ts: float) -> list[dict[str, Any]]:
    """Read feedback_log.jsonl and return records whose ts is >= since_ts."""
    out: list[dict[str, Any]] = []
    if not LOG_PATH.exists():
        return out
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_raw = rec.get("ts")
                try:
                    ts = dt.datetime.fromisoformat(str(ts_raw)).timestamp()
                except Exception:
                    continue
                if ts >= since_ts:
                    out.append(rec)
    except Exception as e:
        log.warning("[nexus_feedback] log read error (%s): %s", type(e).__name__, e)
    return out


def get_stats(window_h: int = 24) -> dict[str, Any]:
    """
    Aggregate stats across the last `window_h` hours.
    Returns:
      {
        "by_kind": {kind: {posts, positive, negative, ratio}},
        "total_posts": int,
        "total_reactions": int,
        "window_h": int,
      }
    `ratio` is positive / (positive + negative), or 0.0 if denominator is 0.
    Neutral reactions are counted in total_reactions but excluded from ratio.
    """
    try:
        try:
            window = int(window_h)
        except (TypeError, ValueError):
            window = 24
        if window <= 0:
            window = 24

        now_ts = _now_ts()
        since_ts = now_ts - (window * 3600)

        # Snapshot stamps under lock
        with _state_lock:
            stamps_snapshot = dict(_stamps)

        by_kind: dict[str, dict[str, Any]] = {}
        total_posts = 0

        for rec in stamps_snapshot.values():
            posted_at = rec.get("posted_at")
            try:
                ts = dt.datetime.fromisoformat(str(posted_at)).timestamp()
            except Exception:
                continue
            if ts < since_ts:
                continue
            kind = str(rec.get("kind") or "unknown")
            bucket = by_kind.setdefault(
                kind, {"posts": 0, "positive": 0, "negative": 0, "ratio": 0.0}
            )
            bucket["posts"] += 1
            total_posts += 1

        # Walk the reaction log once, bucket by kind
        log_records = _iter_log_records(since_ts)
        total_reactions = 0
        for rec in log_records:
            total_reactions += 1
            kind = str(rec.get("kind") or "unknown")
            bucket = by_kind.setdefault(
                kind, {"posts": 0, "positive": 0, "negative": 0, "ratio": 0.0}
            )
            pol = rec.get("polarity")
            if pol == "positive":
                bucket["positive"] += 1
            elif pol == "negative":
                bucket["negative"] += 1
            # neutral counted in total_reactions, not in ratio inputs

        # Compute ratios
        for kind, bucket in by_kind.items():
            pos = int(bucket.get("positive", 0))
            neg = int(bucket.get("negative", 0))
            denom = pos + neg
            bucket["ratio"] = round(pos / denom, 4) if denom else 0.0

        return {
            "by_kind": by_kind,
            "total_posts": total_posts,
            "total_reactions": total_reactions,
            "window_h": window,
        }
    except Exception as e:
        log.warning("[nexus_feedback] get_stats error (%s): %s", type(e).__name__, e)
        return {
            "by_kind": {},
            "total_posts": 0,
            "total_reactions": 0,
            "window_h": int(window_h) if isinstance(window_h, (int, float)) else 24,
        }


__all__ = [
    "install",
    "stamp_chime",
    "on_reaction",
    "get_stats",
    "POSITIVE_EMOJI",
    "NEGATIVE_EMOJI",
    "NEUTRAL_EMOJI",
]
