"""
Nexus morning digest — daily ~9am briefing posted in #dev-logs.

Once a day, between 8:30am and 9:30am local time, Nexus posts a short
"good morning" embed in #dev-logs covering:

  - voice highlights: 3-5 standout moments from the last 24h of
    voice_transcripts.jsonl (curated by Claude haiku so we skip whisper
    hallucinations like repeated "thank you")
  - what nexus did: feedback stats from nexus_feedback.get_stats(24) if
    that module is available (chimes, followups, quotes, reactions)
  - heads-up: 1-2 recent mem0 facts tagged scope="tnc" from last 24h,
    only if substantive

Public API (orchestrator wires these up):
    install(bot)              — log install, prime state
    await maybe_post_daily(bot) -> bool  — called once per caretaker cycle;
                                           self-gates on time window + state

Design notes:
  - All data sources are optional; missing pieces are silently skipped.
  - Single anthropic client, lazy-init.
  - Atomic state writes via tmp+os.replace, guarded by threading.Lock.
  - Entire maybe_post_daily wrapped in try/except so errors never bubble
    up to the caretaker cycle. asyncio.CancelledError always re-raised.
  - Uses local server time for the post window (no timezone conversion).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

import anthropic

import config


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Live-tunable constants
# ---------------------------------------------------------------------------
POST_HOUR: int = 9
POST_WINDOW_MIN: int = 30              # post anywhere from 8:30 to 9:30
MIN_HOURS_BETWEEN: int = 20            # never post twice within 20h
VOICE_HIGHLIGHT_COUNT: int = 5
DEV_LOGS_CHANNEL_NAME: str = "logs"  # real channel is 📝│logs → canon 'logs'

DIGEST_MODEL: str = os.environ.get("DIGEST_MODEL", "claude-haiku-4-5-20251001")
DIGEST_EMBED_COLOR: int = 0x3B82F6     # brand accent blue

ROOT = Path(config.ROOT) if hasattr(config, "ROOT") else Path(__file__).parent
STATE_PATH = ROOT / "digest_state.json"
VOICE_TRANSCRIPTS_PATH = ROOT / "voice_transcripts.jsonl"

_STATE_LOCK = threading.Lock()
_installed = False


# ---------------------------------------------------------------------------
# Anthropic client (lazy)
# ---------------------------------------------------------------------------
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# State helpers — atomic, lock-guarded
# ---------------------------------------------------------------------------
def _default_state() -> dict:
    return {
        "version": 1,
        "last_posted_at": None,
        "last_posted_date": None,
        "post_count": 0,
    }


def _load_state() -> dict:
    with _STATE_LOCK:
        if not STATE_PATH.exists():
            return _default_state()
        try:
            raw = STATE_PATH.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else _default_state()
        except Exception as e:
            log.warning("[nexus_digest] state load error (%s): %s — starting fresh", type(e).__name__, e)
            data = _default_state()
        for k, v in _default_state().items():
            data.setdefault(k, v)
        return data


def _save_state(state: dict) -> None:
    with _STATE_LOCK:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            log.warning("[nexus_digest] state save error (%s): %s", type(e).__name__, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Channel matcher — case-insensitive, emoji-stripped (copied from nexus_eyes)
# ---------------------------------------------------------------------------
_LEAD_STRIP = re.compile(
    r"^[\s_\-\u00a0\u2000-\u206F\u2E00-\u2E7F\u2500-\u257F"
    r"\u2600-\u27BF\U0001F000-\U0001FFFF\u2502|\u00b7\u2022\.:,;]+",
    flags=re.UNICODE,
)


def _canon(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = _LEAD_STRIP.sub("", s)
    return s.strip()


def _find_dev_logs_channel(bot: Any):
    """Find #dev-logs text channel across all guilds. Case-insensitive,
    emoji-prefix tolerant. Returns discord.TextChannel or None."""
    if bot is None:
        return None
    target = _canon(DEV_LOGS_CHANNEL_NAME)
    if not target:
        return None
    try:
        guilds = list(getattr(bot, "guilds", []) or [])
    except Exception:
        return None
    for g in guilds:
        try:
            text_channels = list(getattr(g, "text_channels", []) or [])
        except Exception:
            continue
        # Pass 1: exact canon match
        for ch in text_channels:
            try:
                if _canon(ch.name) == target:
                    return ch
            except Exception:
                continue
        # Pass 2: substring fallback
        for ch in text_channels:
            try:
                if target in _canon(ch.name):
                    return ch
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Time-window gate
# ---------------------------------------------------------------------------
def _in_post_window(now: dt.datetime) -> bool:
    """True if now is within POST_WINDOW_MIN of POST_HOUR:00 local time.

    With POST_HOUR=9 and POST_WINDOW_MIN=30 that's 08:30 <= now <= 09:30.
    """
    center = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
    delta = abs((now - center).total_seconds())
    return delta <= POST_WINDOW_MIN * 60


def _hours_since_last_post(state: dict, now: dt.datetime) -> float:
    last = state.get("last_posted_at")
    if not last:
        return 1e9
    try:
        last_dt = dt.datetime.fromisoformat(str(last))
    except Exception:
        return 1e9
    # both naive (local) — subtract safely
    if last_dt.tzinfo is not None and now.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=None)
    return (now - last_dt).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Voice transcripts reader — last ~500 lines, filter to last 24h
# ---------------------------------------------------------------------------
def _read_recent_voice_lines(hours: int = 24, max_lines: int = 500) -> list[dict]:
    """Read last max_lines of voice_transcripts.jsonl, return entries
    whose ts is within the last `hours`. Graceful skip if file missing."""
    if not VOICE_TRANSCRIPTS_PATH.exists():
        return []
    cutoff_ts = time.time() - hours * 3600
    try:
        # Cheap tail: read whole file, keep last N lines. These files stay small.
        with VOICE_TRANSCRIPTS_PATH.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        log.warning("[nexus_digest] voice transcripts read error (%s): %s", type(e).__name__, e)
        return []

    lines = lines[-max_lines:]
    out: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        try:
            ts = float(entry.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < cutoff_ts:
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Haiku curation — pick standout voice moments
# ---------------------------------------------------------------------------
_HIGHLIGHT_SYSTEM = """you scan 24 hours of voice-channel transcript lines from a small discord and pick the standout moments worth putting in a morning recap.

skip whisper hallucinations — lines that are just repeated "thank you", "bye", "you", " " etc. skip single-word filler. skip anything that reads like transcription noise.

pick moments that show: a real decision or commitment, a funny exchange, a milestone, a confession, a technical insight, a plan, or a meaningful reaction. prefer variety — don't pick 5 lines from the same conversation.

respond with JSON ONLY, no prose, no markdown fences:
{"highlights": [{"speaker": "<name>", "line": "<short quote, <=140 chars>", "why_interesting": "<6-10 words>"}]}

if nothing qualifies, return {"highlights": []}. output must be valid JSON with that one key."""


def _parse_json_loose(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip("` \n")
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


async def _curate_voice_highlights(lines: list[dict], want: int) -> list[dict]:
    """Call haiku to pick standout voice moments. Returns list of
    {speaker, line, why_interesting} dicts, max `want`."""
    if not lines:
        return []

    # Shape a compact text blob for haiku
    rendered_lines: list[str] = []
    for e in lines:
        name = str(e.get("name") or "unknown")
        text = str(e.get("text") or "").strip()
        if not text:
            continue
        if len(text) > 200:
            text = text[:200] + "..."
        rendered_lines.append(f"{name}: {text}")

    if not rendered_lines:
        return []

    # Cap total size — haiku input budget
    blob = "\n".join(rendered_lines[-400:])
    if len(blob) > 12000:
        blob = blob[-12000:]

    user_prompt = (
        f"transcript lines from the last 24 hours:\n\n{blob}\n\n"
        f"pick up to {want} standout moments. respond with JSON."
    )

    try:
        client = _get_client()
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=DIGEST_MODEL,
                max_tokens=700,
                temperature=0.4,
                system=_HIGHLIGHT_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("[nexus_digest] highlight haiku error (%s): %s", type(e).__name__, e)
        return []

    try:
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
    except Exception:
        raw = ""
    data = _parse_json_loose(raw) or {}
    items = data.get("highlights") or []
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        speaker = str(it.get("speaker") or "").strip()[:40]
        line = str(it.get("line") or "").strip()[:160]
        why = str(it.get("why_interesting") or "").strip()[:80]
        if not speaker or not line:
            continue
        out.append({"speaker": speaker, "line": line, "why_interesting": why})
        if len(out) >= want:
            break
    return out


# ---------------------------------------------------------------------------
# Feedback stats (optional — nexus_feedback may not exist yet)
# ---------------------------------------------------------------------------
def _try_feedback_stats(hours: int = 24) -> Optional[dict]:
    """Call nexus_feedback.get_stats(hours) if available, else None."""
    try:
        import nexus_feedback  # type: ignore
    except Exception:
        return None
    fn = getattr(nexus_feedback, "get_stats", None)
    if fn is None:
        return None
    try:
        return fn(hours)
    except Exception as e:
        log.warning("[nexus_digest] feedback stats error (%s): %s", type(e).__name__, e)
        return None


def _format_feedback_stats(stats: Optional[dict]) -> Optional[str]:
    """Render feedback stats dict into a single inline line.
    Returns None if stats unusable."""
    if not isinstance(stats, dict):
        return None

    # Try common shape first: {"chimes": 5, "followups": 3, "quotes": 2, "vision_reacts": 1, "positive_pct": 78}
    parts: list[str] = []
    for key, label in [
        ("chimes", "chimes"),
        ("followups", "followups"),
        ("quotes", "quotes"),
        ("vision_reacts", "vision react"),
        ("vision", "vision react"),
    ]:
        if key in stats:
            try:
                n = int(stats[key])
                if n > 0:
                    suffix = "" if n == 1 or label.endswith("s") else "s"
                    parts.append(f"{n} {label}{suffix}")
            except (TypeError, ValueError):
                pass

    pct_val: Optional[float] = None
    for key in ("positive_pct", "positive_percent", "pct_positive"):
        if key in stats:
            try:
                pct_val = float(stats[key])
                break
            except (TypeError, ValueError):
                pass

    if not parts and pct_val is None:
        return None

    body = " · ".join(parts) if parts else "no activity"
    if pct_val is not None:
        pct_str = f"{pct_val:.0f}%" if pct_val > 1 else f"{pct_val*100:.0f}%"
        body = f"{body} · {pct_str} positive"
    return body


# ---------------------------------------------------------------------------
# Heads-up — recent scope=tnc mem0 facts
# ---------------------------------------------------------------------------
def _get_mem0_and_lock():
    """Return (mem0_client, lock) tuple, or (None, None) if unavailable."""
    try:
        import nexus_brain  # type: ignore
    except Exception:
        return None, None
    try:
        m = nexus_brain._get_mem0()
    except Exception as e:
        log.warning("[nexus_digest] mem0 init error (%s): %s", type(e).__name__, e)
        return None, None
    lock = getattr(nexus_brain, "_MEM0_LOCK", threading.Lock())
    return m, lock


def _recent_tnc_facts(hours: int = 24, limit: int = 2) -> list[str]:
    """Search mem0 for recent scope=tnc facts. Returns list of short strings
    suitable for the heads-up section. Read-only."""
    m, lock = _get_mem0_and_lock()
    if m is None:
        return []

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

    try:
        with lock:
            results = m.search(
                query="recent tnc nexus collective notes",
                filters={"agent_id": "nexus"},
                limit=40,
            )
        mems = results.get("results", []) if isinstance(results, dict) else results
        mems = mems or []
    except Exception as e:
        log.warning("[nexus_digest] mem0 search error (%s): %s", type(e).__name__, e)
        return []

    picks: list[tuple[dt.datetime, str]] = []
    for mem in mems:
        md = mem.get("metadata") or {}
        if md.get("scope") != "tnc":
            continue
        # Skip the noisy tags — followups / skills / quotes live their own lives
        tag = str(md.get("tag") or "")
        if tag in ("followup", "skill", "quote"):
            continue

        # Find a timestamp
        created_iso = md.get("created_at") or mem.get("created_at") or mem.get("updated_at")
        try:
            created = dt.datetime.fromisoformat(str(created_iso))
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if created < cutoff:
            continue

        memory_text = str(mem.get("memory") or mem.get("text") or "").strip()
        if not memory_text:
            continue
        if len(memory_text) > 180:
            memory_text = memory_text[:180].rstrip() + "..."

        picks.append((created, memory_text))

    # newest first
    picks.sort(key=lambda p: p[0], reverse=True)
    return [text for _, text in picks[:limit]]


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------
def _build_embed(
    today_str: str,
    now: dt.datetime,
    highlights: list[dict],
    feedback_line: Optional[str],
    heads_up: list[str],
) -> Any:
    """Build the discord.Embed. Returns None if discord isn't usable."""
    try:
        import discord  # type: ignore
    except Exception as e:
        log.warning("[nexus_digest] discord import failed (%s): %s", type(e).__name__, e)
        return None

    # Title kept lowercase apart from the sunrise emoji — explicitly allowed here
    embed = discord.Embed(
        title=f"\U0001F305 nexus morning digest \u2014 {today_str}",
        color=DIGEST_EMBED_COLOR,
    )

    # Voice highlights field
    if highlights:
        bullets: list[str] = []
        for h in highlights[:VOICE_HIGHLIGHT_COUNT]:
            speaker = h.get("speaker", "?")
            line = h.get("line", "").replace("\n", " ")
            bullets.append(f"\u2022 **{speaker}:** {line}")
        value = "\n".join(bullets)
        if len(value) > 1024:
            value = value[:1020] + "..."
        embed.add_field(
            name="\U0001F399\uFE0F voice highlights",
            value=value,
            inline=False,
        )

    # Yesterday's activity
    if feedback_line:
        value = feedback_line
        if len(value) > 1024:
            value = value[:1020] + "..."
        embed.add_field(
            name="\U0001F4CA yesterday's activity",
            value=value,
            inline=False,
        )

    # Heads-up
    if heads_up:
        value = "\n".join(f"\u2022 {h}" for h in heads_up[:2])
        if len(value) > 1024:
            value = value[:1020] + "..."
        embed.add_field(
            name="\U0001F4A1 heads-up",
            value=value,
            inline=False,
        )

    embed.set_footer(text=f"nexus \u00b7 auto-posted at {now.strftime('%H:%M')}")
    return embed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_warned_no_channel = False


def install(bot: Any) -> None:
    """Idempotent. Loads state file and logs install line."""
    global _installed
    if _installed:
        return
    _installed = True
    state = _load_state()
    log.info(
        "[nexus_digest] installed — post_hour=%d window_min=%d min_hours_between=%d "
        "highlight_count=%d channel=%s model=%s post_count=%d last_date=%s",
        POST_HOUR,
        POST_WINDOW_MIN,
        MIN_HOURS_BETWEEN,
        VOICE_HIGHLIGHT_COUNT,
        DEV_LOGS_CHANNEL_NAME,
        DIGEST_MODEL,
        int(state.get("post_count") or 0),
        state.get("last_posted_date"),
    )


async def maybe_post_daily(bot: Any) -> bool:
    """Called by caretaker cycle once per cycle. Self-gates on time window
    and state. Returns True if it posted, False otherwise. Never raises."""
    global _warned_no_channel
    try:
        now = dt.datetime.now()  # local server time — no tz conversion
        today_str = now.strftime("%Y-%m-%d")

        # Gate 1: time window (8:30am - 9:30am local)
        if not _in_post_window(now):
            return False

        # Gate 2: state (not posted today, >MIN_HOURS_BETWEEN since last)
        state = _load_state()
        if state.get("last_posted_date") == today_str:
            return False
        if _hours_since_last_post(state, now) < MIN_HOURS_BETWEEN:
            return False

        # Gate 3: resolve #dev-logs channel
        channel = _find_dev_logs_channel(bot)
        if channel is None:
            if not _warned_no_channel:
                log.warning(
                    "[nexus_digest] #%s channel not found — skipping digest post",
                    DEV_LOGS_CHANNEL_NAME,
                )
                _warned_no_channel = True
            return False

        # Build content — every source is optional; failures degrade gracefully
        voice_lines = _read_recent_voice_lines(hours=24, max_lines=500)
        highlights: list[dict] = []
        if voice_lines:
            try:
                highlights = await _curate_voice_highlights(
                    voice_lines, VOICE_HIGHLIGHT_COUNT
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "[nexus_digest] highlight curation failed (%s): %s",
                    type(e).__name__, e,
                )
                highlights = []

        feedback_stats = await asyncio.to_thread(_try_feedback_stats, 24)
        feedback_line = _format_feedback_stats(feedback_stats)

        heads_up: list[str] = []
        try:
            heads_up = await asyncio.to_thread(_recent_tnc_facts, 24, 2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "[nexus_digest] heads-up fetch error (%s): %s", type(e).__name__, e,
            )

        # If we have literally nothing to say, skip rather than post an empty card
        if not highlights and not feedback_line and not heads_up:
            log.info("[nexus_digest] no content across all sections — skipping today")
            # Still record a soft skip? No — leave state alone so a later cycle can try
            return False

        # Build + post
        embed = _build_embed(today_str, now, highlights, feedback_line, heads_up)
        if embed is None:
            return False

        try:
            await channel.send(embed=embed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "[nexus_digest] post to #%s failed (%s): %s",
                getattr(channel, "name", "?"), type(e).__name__, e,
            )
            return False

        # Update state — atomic
        state["last_posted_at"] = now.isoformat(timespec="seconds")
        state["last_posted_date"] = today_str
        try:
            state["post_count"] = int(state.get("post_count") or 0) + 1
        except (TypeError, ValueError):
            state["post_count"] = 1
        _save_state(state)

        log.info(
            "[nexus_digest] posted — channel=#%s highlights=%d feedback=%s heads_up=%d post_count=%d",
            getattr(channel, "name", "?"),
            len(highlights),
            "yes" if feedback_line else "no",
            len(heads_up),
            state["post_count"],
        )
        return True

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("[nexus_digest] maybe_post_daily fatal (%s): %s", type(e).__name__, e)
        return False


__all__ = [
    "install",
    "maybe_post_daily",
    "POST_HOUR",
    "POST_WINDOW_MIN",
    "MIN_HOURS_BETWEEN",
    "VOICE_HIGHLIGHT_COUNT",
    "DEV_LOGS_CHANNEL_NAME",
    "DIGEST_MODEL",
    "DIGEST_EMBED_COLOR",
]
