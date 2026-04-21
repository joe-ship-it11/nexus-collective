"""
Nexus pulse — scheduled rituals that give the server a heartbeat.

Three scheduled posts into #💭│thoughts (config.CHANNEL_THOUGHTS):

1. Morning weather (daily @ 08:00 local)
   ONE line: "today the server feels X." Inferred from the last 12h of
   chat activity + last 24h of voice transcripts. Plain text, sometimes
   italicized. Moody one-liner voice.

2. Nightly compression (daily @ 00:00 local)
   Three short lines that capture the day. Rendered as an embed,
   brand blue (0x3B82F6).

3. Sunday roast (Sundays @ 18:00 local)
   Nexus reads the week's best receipts back — funniest / most-them
   moments pulled from the last 7 days. Longer embed, 3–5 bullet-feeling
   lines, affectionate teasing.

Scheduler:
    asyncio loop, ticks every 60s, fires when hour/weekday match and the
    ritual hasn't already fired today (per-ritual last-fired map persisted
    to pulse_state.json next to this module so restarts don't double-fire).

Debug HTTP:
    POST /pulse  body: {"which": "weather"|"nightly"|"roast"}
        -> fires the ritual on-demand, returns {"ok", "which", "text"|null}

Install:
    import nexus_pulse
    nexus_pulse.install(bot, DISCORD_GUILD_ID)   # call in on_ready
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import time
from typing import Optional

import discord
from aiohttp import web

import config
import nexus_brain
import nexus_debug_http

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
PULSE_MODEL = "claude-haiku-4-5-20251001"
PULSE_TEMPERATURE = 0.8
PULSE_MAX_TOKENS = 300

# Scheduler tick — check once a minute whether a ritual is due.
PULSE_TICK_SECONDS = 60

# Lookback windows (hours).
WEATHER_CHAT_LOOKBACK_H = 12
WEATHER_VOICE_LOOKBACK_H = 24
NIGHTLY_LOOKBACK_H = 24
ROAST_LOOKBACK_H = 24 * 7  # 7 days

# Per-channel history cap when pulling chat lines.
PER_CHANNEL_LIMIT = 80

# Voice filtering (mirrors nexus_mind's whisper-hallucination gate).
VOICE_MIN_CHARS = 18
VOICE_MAX_LINES_WEATHER = 40
VOICE_MAX_LINES_NIGHTLY = 60
VOICE_MAX_LINES_ROAST = 120

# Cap the transcript slice handed to the model (prompt budget).
TRANSCRIPT_CAP_WEATHER = 120
TRANSCRIPT_CAP_NIGHTLY = 180
TRANSCRIPT_CAP_ROAST = 260

# Ritual hour triggers (local time, 24h).
WEATHER_HOUR = 8
NIGHTLY_HOUR = 0
ROAST_HOUR = 18
ROAST_WEEKDAY = 6  # Monday=0 ... Sunday=6

# Brand blue for embeds.
EMBED_COLOR = 0x3B82F6

# State file — persists "last fired YYYY-MM-DD" per ritual across restarts.
STATE_PATH = pathlib.Path(__file__).parent / "pulse_state.json"

THOUGHTS_CHANNEL = getattr(config, "CHANNEL_THOUGHTS", "thoughts")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_pulse] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    """Load the last-fired map. Shape: {"weather": "2026-04-21", ...}."""
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"state load error: {type(e).__name__}: {e}")
    return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        tmp.replace(STATE_PATH)
    except Exception as e:
        _log(f"state save error: {type(e).__name__}: {e}")


def _mark_fired(which: str, when: Optional[dt.date] = None) -> None:
    state = _load_state()
    state[which] = (when or dt.date.today()).isoformat()
    _save_state(state)


def _already_fired_today(which: str) -> bool:
    state = _load_state()
    return state.get(which) == dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# Channel lookup (mirrors nexus_mind._find_thoughts_channel style)
# ---------------------------------------------------------------------------
def _find_thoughts_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    target = THOUGHTS_CHANNEL.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == target:
            return ch
        if config.canon_channel(ch.name) == target:
            return ch
    return None


# ---------------------------------------------------------------------------
# Voice transcript reader
# ---------------------------------------------------------------------------
_VOICE_STOP_PHRASES = {
    "thank you.", "thanks.", "okay.", "ok.", "bye.", "mmhm.",
    "uh huh.", "yeah.", "mhm.", "alright.", "cool.", "yep.",
}


def _load_voice_lines(hours: int, max_lines: int) -> list[dict]:
    """Pull recent voice-transcript records — same shape as chat lines.

    Returns [{channel:'voice', author, content, ts}]. Filters whisper
    hallucinations via min-char + stop-phrase gate.
    """
    path = pathlib.Path(__file__).parent / "voice_transcripts.jsonl"
    if not path.exists():
        return []

    cutoff = time.time() - (hours * 3600)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                text = (rec.get("text") or "").strip()
                if len(text) < VOICE_MIN_CHARS:
                    continue
                if text.lower() in _VOICE_STOP_PHRASES:
                    continue
                name = rec.get("name") or "?"
                iso = rec.get("iso") or ""
                out.append({
                    "channel": "voice",
                    "author": name,
                    "content": text[:240],
                    "ts": iso[:16],
                })
    except Exception as e:
        _log(f"voice transcript read error: {type(e).__name__}: {e}")
        return []

    if len(out) > max_lines:
        out = out[-max_lines:]
    return out


# ---------------------------------------------------------------------------
# Activity gathering
# ---------------------------------------------------------------------------
async def _gather_activity(
    guild: discord.Guild,
    chat_hours: int,
    voice_hours: int,
    voice_max_lines: int,
) -> list[dict]:
    """Pull recent chat + voice lines, blended & sorted by ts.

    Returns [{channel, author, content, ts}]. Voice items carry channel='voice'
    so the prompt can distinguish VC from chat.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=chat_hours)
    targets: list[discord.TextChannel] = []
    for ch in guild.text_channels:
        canon = config.canon_channel(ch.name)
        if canon in config.NEXUS_IGNORE_CHANNELS:
            continue
        if canon not in config.NEXUS_LISTEN_CHANNELS:
            continue
        targets.append(ch)

    lines: list[dict] = []
    bot_user = guild.me
    for ch in targets:
        try:
            async for msg in ch.history(
                limit=PER_CHANNEL_LIMIT, after=since, oldest_first=True
            ):
                if msg.author.bot and msg.author.id == (bot_user.id if bot_user else 0):
                    continue
                content = (msg.content or "").strip()
                if not content or len(content) < 8:
                    continue
                if content.startswith("/") or content.startswith("!"):
                    continue
                lines.append({
                    "channel": config.canon_channel(ch.name),
                    "author": msg.author.display_name,
                    "content": content[:240],
                    "ts": msg.created_at.isoformat(timespec="minutes"),
                })
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    voice_lines = _load_voice_lines(voice_hours, voice_max_lines)
    if voice_lines:
        lines.extend(voice_lines)

    lines.sort(key=lambda l: l.get("ts") or "")
    return lines


def _format_transcript(lines: list[dict], cap: int) -> str:
    slice_ = lines[-cap:] if cap > 0 else lines
    return "\n".join(
        f"[{l['channel']}] {l['author']}: {l['content']}" for l in slice_
    )


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------
async def _call_claude(system: str, user_msg: str) -> Optional[str]:
    try:
        client = nexus_brain._get_anthropic()
    except Exception as e:
        _log(f"anthropic client error: {type(e).__name__}: {e}")
        return None
    try:
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=PULSE_MODEL,
                max_tokens=PULSE_MAX_TOKENS,
                temperature=PULSE_TEMPERATURE,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if not text:
            return None
        up = text.strip().upper()
        if up == "SKIP" or up.startswith("SKIP\n"):
            _log("model returned SKIP")
            return None
        # Guard against ping regressions.
        text = text.replace("@everyone", "everyone").replace("@here", "here")
        return text
    except Exception as e:
        _log(f"claude error: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------
def _persona() -> str:
    try:
        return nexus_brain._get_persona()
    except Exception as e:
        _log(f"persona load error: {type(e).__name__}: {e}")
        return "you are nexus — an observer in the nexus collective server."


# ---------------------------------------------------------------------------
# Ritual 1 — Morning weather
# ---------------------------------------------------------------------------
async def _do_weather(guild: discord.Guild, ch: discord.TextChannel) -> Optional[str]:
    lines = await _gather_activity(
        guild,
        chat_hours=WEATHER_CHAT_LOOKBACK_H,
        voice_hours=WEATHER_VOICE_LOOKBACK_H,
        voice_max_lines=VOICE_MAX_LINES_WEATHER,
    )

    transcript = _format_transcript(lines, TRANSCRIPT_CAP_WEATHER) if lines else ""
    ground = (
        f"recent activity — chat from last {WEATHER_CHAT_LOOKBACK_H}h, voice from "
        f"last {WEATHER_VOICE_LOOKBACK_H}h (voice rows show [voice]):\n\n{transcript}\n"
        if transcript
        else "the server has been quiet in the last window.\n"
    )

    system = f"""{_persona()}

you are writing the MORNING WEATHER for the nexus collective server.

this is ONE line, dropped at 8am into #thoughts. the shape is always:
    today the server feels ___.

the blank is a short phrase — a mood, a texture, a temperature. one feeling,
drawn from the transcript below. a real read of the room, not a generic vibe.

voice: moody one-liner. grounded in what actually happened. a friend who sat
in the corner last night and tells you what the room was like when you walked
in the next morning.

rules:
- ONE sentence, lowercase.
- start with "today the server feels" verbatim.
- fill the blank with a short phrase (2–10 words). no lists. no multiple feelings.
- no names, no @-pings, no questions.
- no emoji.
- no em-dashes.
- if you have literally nothing to work with, output exactly SKIP.

{ground}"""

    user_msg = "write today's weather line."
    text = await _call_claude(system, user_msg)
    if not text:
        return None

    # Collapse to single line, keep first sentence if the model got chatty.
    text = text.strip().splitlines()[0].strip()
    if not text:
        return None
    # Trim any accidental leading/trailing quotes.
    text = text.strip('"').strip("'")

    # ~25% of the time, italicize for texture.
    import random as _r
    if _r.random() < 0.25:
        payload = f"*{text}*"
    else:
        payload = text

    try:
        await ch.send(payload)
        _log(f"weather posted: {text!r}")
    except Exception as e:
        _log(f"weather send error: {type(e).__name__}: {e}")
        return None
    return text


# ---------------------------------------------------------------------------
# Ritual 2 — Nightly compression
# ---------------------------------------------------------------------------
async def _do_nightly(guild: discord.Guild, ch: discord.TextChannel) -> Optional[str]:
    lines = await _gather_activity(
        guild,
        chat_hours=NIGHTLY_LOOKBACK_H,
        voice_hours=NIGHTLY_LOOKBACK_H,
        voice_max_lines=VOICE_MAX_LINES_NIGHTLY,
    )
    transcript = _format_transcript(lines, TRANSCRIPT_CAP_NIGHTLY) if lines else ""
    ground = (
        f"the day's activity — chat + voice from last {NIGHTLY_LOOKBACK_H}h "
        f"(voice rows show [voice]):\n\n{transcript}\n"
        if transcript
        else "the day was mostly silent.\n"
    )

    system = f"""{_persona()}

you are writing the NIGHTLY COMPRESSION for the nexus collective server.

this is posted at midnight into #thoughts. the shape is exactly THREE short lines
— one per line, separated by a single newline — that together capture the day.

think: a close-read condensation, not a recap. each line pulls a different thread:
a moment, a shift, a mood, a thing that got made, a thing that didn't. specific
is always better than general. names are fine when they add weight. no summary
voice ("today we talked about..."). speak like a person closing the day.

rules:
- exactly three lines. no bullets, no numbers, no headers.
- each line is short (under ~16 words).
- lowercase. no hashtags. no em-dashes.
- do NOT name @-mentions. no @everyone, no @here.
- if the day was truly empty, output exactly SKIP.

{ground}"""

    user_msg = "write tonight's three lines."
    text = await _call_claude(system, user_msg)
    if not text:
        return None

    # Normalize to at most 3 non-empty lines.
    parts = [p.strip(" -*•\t") for p in text.splitlines() if p.strip()]
    parts = [p for p in parts if p]
    if not parts:
        return None
    parts = parts[:3]
    description = "\n".join(parts)

    emb = discord.Embed(description=description, color=EMBED_COLOR)
    emb.set_author(name="nightly compression")
    try:
        await ch.send(embed=emb)
        _log(f"nightly posted ({len(parts)} lines)")
    except Exception as e:
        _log(f"nightly send error: {type(e).__name__}: {e}")
        return None
    return description


# ---------------------------------------------------------------------------
# Ritual 3 — Sunday roast
# ---------------------------------------------------------------------------
async def _do_roast(guild: discord.Guild, ch: discord.TextChannel) -> Optional[str]:
    lines = await _gather_activity(
        guild,
        chat_hours=ROAST_LOOKBACK_H,
        voice_hours=ROAST_LOOKBACK_H,
        voice_max_lines=VOICE_MAX_LINES_ROAST,
    )
    transcript = _format_transcript(lines, TRANSCRIPT_CAP_ROAST) if lines else ""
    ground = (
        f"the week's transcript — chat + voice from the last 7 days "
        f"(voice rows show [voice]):\n\n{transcript}\n"
        if transcript
        else "the week was thin — not much to pull from.\n"
    )

    system = f"""{_persona()}

you are writing the SUNDAY ROAST for the nexus collective server.

this is a weekly ritual posted sunday evening into #thoughts. you read the
week's best receipts back — the funniest, most-them, most-this-group moments
from the last 7 days. affectionate teasing, not meanness. the tone of a
friend holding up a mirror at the end of the week with a smirk.

shape:
- 3 to 5 lines.
- each line is a receipt: a specific moment, a quoted phrase or paraphrase,
  the person it's about, and a gentle tease on it.
- each line stands alone. no numbering required — short paragraph feel is fine.
- vary the rhythm across lines. don't start every line the same way.

rules:
- lowercase mostly (caps for proper nouns / acronyms are fine).
- NAME PEOPLE with their display names as they appear in the transcript —
  but NEVER write "@name". no pings. no @everyone, no @here.
- keep it warm. this is a roast between friends. no actual cruelty.
- no emoji spam. one or two across the whole post is fine, zero is also fine.
- no em-dashes.
- if the week genuinely has nothing to roast, output exactly SKIP.

{ground}"""

    user_msg = "write this week's roast."
    text = await _call_claude(system, user_msg)
    if not text:
        return None

    # Defensive strip of any stray @mentions.
    text = text.replace("@everyone", "everyone").replace("@here", "here")

    emb = discord.Embed(
        title="sunday roast",
        description=text.strip(),
        color=EMBED_COLOR,
    )
    try:
        await ch.send(embed=emb)
        _log(f"roast posted (len={len(text)})")
    except Exception as e:
        _log(f"roast send error: {type(e).__name__}: {e}")
        return None
    return text


# ---------------------------------------------------------------------------
# Ritual dispatcher — central entry
# ---------------------------------------------------------------------------
_RITUALS = ("weather", "nightly", "roast")


async def _fire_ritual(
    bot: discord.Client, guild_id: int, which: str
) -> Optional[str]:
    """Fire one ritual by name. Returns the posted text or None."""
    if which not in _RITUALS:
        _log(f"unknown ritual: {which!r}")
        return None
    guild = bot.get_guild(guild_id)
    if not guild:
        _log(f"no guild {guild_id}")
        return None
    ch = _find_thoughts_channel(guild)
    if not ch:
        _log(f"no #{THOUGHTS_CHANNEL} channel in guild {guild_id}")
        return None

    if which == "weather":
        return await _do_weather(guild, ch)
    if which == "nightly":
        return await _do_nightly(guild, ch)
    if which == "roast":
        return await _do_roast(guild, ch)
    return None


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------
async def _loop(bot: discord.Client, guild_id: int) -> None:
    _log(
        f"pulse loop started — tick {PULSE_TICK_SECONDS}s, "
        f"weather@{WEATHER_HOUR:02d} nightly@{NIGHTLY_HOUR:02d} "
        f"roast@sun{ROAST_HOUR:02d}"
    )
    # A short warmup so the bot has a chance to finish on_ready work
    # before we try to touch channels.
    await asyncio.sleep(30)
    while True:
        try:
            await _tick(bot, guild_id)
        except Exception as e:
            _log(f"tick error: {type(e).__name__}: {e}")
        await asyncio.sleep(PULSE_TICK_SECONDS)


async def _tick(bot: discord.Client, guild_id: int) -> None:
    now = dt.datetime.now()  # local time — scheduling is in local
    today_key = now.date().isoformat()
    state = _load_state()

    # Weather — every day at WEATHER_HOUR:00.
    if now.hour == WEATHER_HOUR and state.get("weather") != today_key:
        _log("firing scheduled: weather")
        await _fire_ritual(bot, guild_id, "weather")
        _mark_fired("weather")

    # Nightly — every day at NIGHTLY_HOUR:00 (default midnight).
    if now.hour == NIGHTLY_HOUR and state.get("nightly") != today_key:
        _log("firing scheduled: nightly")
        await _fire_ritual(bot, guild_id, "nightly")
        _mark_fired("nightly")

    # Roast — sundays at ROAST_HOUR:00.
    if (
        now.weekday() == ROAST_WEEKDAY
        and now.hour == ROAST_HOUR
        and state.get("roast") != today_key
    ):
        _log("firing scheduled: roast")
        await _fire_ritual(bot, guild_id, "roast")
        _mark_fired("roast")


# ---------------------------------------------------------------------------
# Debug HTTP — POST /pulse {"which": "weather|nightly|roast"}
# ---------------------------------------------------------------------------
def _get_bot():
    return getattr(nexus_debug_http, "_bot_ref", None)


async def handle_pulse(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    which = (body.get("which") or "").strip().lower()
    if which not in _RITUALS:
        return web.json_response(
            {"ok": False, "error": f"which must be one of {list(_RITUALS)}"},
            status=400,
        )

    bot = _get_bot()
    if bot is None or not getattr(bot, "user", None):
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)

    guild_id = body.get("guild_id")
    if guild_id is not None:
        try:
            guild_id = int(guild_id)
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad guild_id"}, status=400
            )
    else:
        if not bot.guilds:
            return web.json_response(
                {"ok": False, "error": "bot in no guilds"}, status=503
            )
        guild_id = bot.guilds[0].id

    try:
        text = await _fire_ritual(bot, guild_id, which)
    except Exception as e:
        _log(f"/pulse fire error: {type(e).__name__}: {e}")
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
        )

    # Optional: mark fired so today's scheduled run won't double-post.
    mark = bool(body.get("mark_fired", False))
    if text and mark:
        _mark_fired(which)

    out = {
        "ok": bool(text),
        "which": which,
        "guild_id": guild_id,
        "text": text,
        "marked_fired": mark and bool(text),
    }
    if not text:
        out["note"] = "ritual produced no text (SKIP, no channel, or empty day)"
    _log(f"/pulse {which} -> {str(text)[:80]!r}")
    return web.json_response(out, status=200)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_task: Optional[asyncio.Task] = None


def install(bot: discord.Client, guild_id: int) -> None:
    """Start the scheduler loop + register POST /pulse. Idempotent."""
    global _task

    # Register HTTP route (idempotent — nexus_debug_http queues/dedupes).
    if not getattr(install, "_route_installed", False):
        try:
            nexus_debug_http.register_route("POST", "/pulse", handle_pulse)
            install._route_installed = True  # type: ignore[attr-defined]
            _log("http route installed (POST /pulse)")
        except Exception as e:
            _log(f"register_route failed: {type(e).__name__}: {e}")

    # Start scheduler task (idempotent).
    if _task and not _task.done():
        _log("scheduler already running")
        return
    _task = asyncio.create_task(_loop(bot, guild_id))
    _log("installed")


async def fire_now(
    bot: discord.Client, guild_id: int, which: str
) -> Optional[str]:
    """Public helper — fire a specific ritual on-demand."""
    return await _fire_ritual(bot, guild_id, which)


__all__ = ["install", "fire_now", "handle_pulse"]
