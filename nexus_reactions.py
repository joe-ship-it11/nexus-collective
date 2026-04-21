"""
Nexus reaction layer — sparse emoji reactions on messages.

Adds cheap "i'm here, i'm listening" signals without a reply. Haiku classifier
decides if a message deserves a reaction AND which emoji to use, picking from
server custom emoji + a unicode allow-list tuned to TNC vibe.

Pipeline per non-triggered message:
    basic skip -> cooldown -> budget -> consent -> classifier -> add_reaction

Cost: one Haiku call per eligible message (with 60s identical-text cache). Much
cheaper than nexus_proactive — just an emoji decision, not a full reply.

Public API:
    install(bot, guild_id)
    await try_react(message)
    get_stats()
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Optional

import discord

try:
    import nexus_consent
except Exception:
    nexus_consent = None  # type: ignore[assignment]

from anthropic import Anthropic


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Per-channel cooldown between reactions
CHANNEL_COOLDOWN_S = _env_int("NEXUS_REACTIONS_CHANNEL_COOLDOWN_S", 90)

# Server-wide rolling 24h cap
DAILY_CAP = _env_int("NEXUS_REACTIONS_DAILY_CAP", 40)

# Classifier confidence threshold (slightly lower than proactive — reactions are low-risk)
CONFIDENCE_THRESHOLD = _env_float("NEXUS_REACTIONS_CONFIDENCE", 0.65)

# Identical-text classifier cache duration (seconds)
CLASSIFIER_CACHE_TTL_S = 60

# Rolling window for daily cap
DAILY_WINDOW_S = 24 * 60 * 60

# Haiku model for the classifier gate
CLASSIFIER_MODEL = os.environ.get(
    "NEXUS_REACTIONS_MODEL", "claude-haiku-4-5-20251001"
)

# Min message length to consider
MIN_MESSAGE_CHARS = 8

# How much recent context to pull
RECENT_CONTEXT_LIMIT = 6

# Unicode allow-list — tuned to TNC vibe, not fellow-kids energy.
# Maps shortname (what classifier returns) -> unicode char (what we add).
UNICODE_EMOJI: dict[str, str] = {
    "fire":     "🔥",
    "skull":    "💀",
    "eyes":     "👀",
    "salute":   "🫡",
    "sob":      "😭",
    "brain":    "🧠",
    "think":    "🤔",
    "clap":     "👏",
    "100":      "💯",
    "heart":    "🖤",
    "sparkle":  "✨",
    "mirror":   "🪞",
    "thread":   "🧵",
    "moon":     "🌙",
    "bolt":     "⚡",
    "candle":   "🕯️",
    "spiral":   "🌀",
    "pensive":  "😔",
    "smirk":    "😏",
    "real":     "🫠",
}


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_bot: Optional[discord.Client] = None
_guild_id: Optional[int] = None

_state_lock = threading.Lock()

_channel_last_react: dict[int, float] = {}
_react_timestamps: deque[float] = deque()

_stats = {
    "classifier_calls_today": 0,
    "reactions_today": 0,
    "reactions_by_emoji": {},
    "last_react_ts": 0.0,
    "classifier_calls_ts": deque(),
}

_classifier_cache: dict[str, tuple[float, dict]] = {}
_client: Optional[Anthropic] = None


def _log(msg: str) -> None:
    print(f"[nexus_reactions] {msg}", flush=True)


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# Consent stubs
# ---------------------------------------------------------------------------
def _consent_is_quiet() -> bool:
    if nexus_consent is None:
        return False
    try:
        fn = getattr(nexus_consent, "is_quiet", None)
        return bool(fn()) if fn else False
    except Exception:
        return False


def _consent_is_shy(user_id: int) -> bool:
    if nexus_consent is None:
        return False
    try:
        fn = getattr(nexus_consent, "is_shy", None)
        return bool(fn(user_id)) if fn else False
    except Exception:
        return False


def _consent_is_opted_out(user_id: int) -> bool:
    if nexus_consent is None:
        return False
    try:
        fn = getattr(nexus_consent, "is_opted_out", None)
        return bool(fn(user_id)) if fn else False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def _prune_deque(dq: deque, cutoff: float) -> None:
    while dq and dq[0] < cutoff:
        dq.popleft()


def _budget_available() -> bool:
    now = time.time()
    with _state_lock:
        _prune_deque(_react_timestamps, now - DAILY_WINDOW_S)
        return len(_react_timestamps) < DAILY_CAP


def _channel_cooldown_ok(channel_id: int) -> bool:
    now = time.time()
    with _state_lock:
        last = _channel_last_react.get(channel_id, 0.0)
        return (now - last) >= CHANNEL_COOLDOWN_S


def _record_reaction(channel_id: int, emoji_label: str) -> None:
    now = time.time()
    with _state_lock:
        _react_timestamps.append(now)
        _prune_deque(_react_timestamps, now - DAILY_WINDOW_S)
        _channel_last_react[channel_id] = now
        _stats["last_react_ts"] = now
        _stats["reactions_by_emoji"][emoji_label] = (
            _stats["reactions_by_emoji"].get(emoji_label, 0) + 1
        )
        _stats["reactions_today"] = len(_react_timestamps)


# ---------------------------------------------------------------------------
# Emoji resolution
# ---------------------------------------------------------------------------
def _resolve_emoji(shortname: str, guild: Optional[discord.Guild]):
    """Return either a str (unicode) or discord.Emoji (custom), or None if unknown."""
    if not shortname:
        return None
    key = shortname.strip().lower().lstrip(":").rstrip(":")
    # Unicode first
    if key in UNICODE_EMOJI:
        return UNICODE_EMOJI[key]
    # Custom server emoji
    if guild is not None:
        for em in guild.emojis:
            if em.name.lower() == key:
                return em
    return None


def _available_shortnames(guild: Optional[discord.Guild]) -> list[str]:
    names = list(UNICODE_EMOJI.keys())
    if guild is not None:
        names.extend(em.name for em in guild.emojis if em.available)
    return names


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
_CLASSIFIER_SYSTEM = """you decide whether nexus (the ai member of the nexus collective discord) should drop a single emoji reaction on a message.

return JSON ONLY:
{"react": true|false, "emoji": "<shortname>|null", "confidence": 0.0-1.0, "reason": "<short>"}

you'll get a list of available emoji shortnames. pick ONE if reacting — unicode shortname (e.g. "fire", "skull") or a server custom emoji name. if none fit, react=false.

WHEN TO REACT (target ~1 in 10 messages, be sparse):
- banger take, actually funny line, clean burn → "fire" "skull" "100" "smirk"
- someone shared a win, finished a thing → "fire" "clap" "salute"
- vulnerable/honest moment, real emotion → "heart" "pensive" "real"
- bold plan, audacious claim → "eyes" "smirk"
- devastating self-own or fail story → "skull" "sob"
- sharp observation, thinking out loud well → "brain" "think"
- quote-worthy or threading-worthy → "thread" "mirror"

WHEN TO SKIP:
- one-line acks ("ok", "yep", "lol", "true") — skip
- neutral statements, logistics, small talk — skip
- already has reactions from other people — still fine to react if it actually deserves one, but bar is higher
- anything that would feel performative — skip
- questions being asked AT nexus — let the reply path handle it
- anything negative about a specific person — skip

confidence:
- 0.9+ obvious, nexus would feel absent if it skipped
- 0.7-0.89 solid, lean in
- <0.65 skip, output react=false

output must be valid JSON. no markdown, no prose."""


def _classifier_cache_get(text: str) -> Optional[dict]:
    now = time.time()
    with _state_lock:
        expired = [k for k, (exp, _) in _classifier_cache.items() if exp < now]
        for k in expired:
            _classifier_cache.pop(k, None)
        hit = _classifier_cache.get(text)
        if hit and hit[0] >= now:
            return dict(hit[1])
    return None


def _classifier_cache_put(text: str, decision: dict) -> None:
    with _state_lock:
        _classifier_cache[text] = (time.time() + CLASSIFIER_CACHE_TTL_S, dict(decision))


def _bump_classifier_counter() -> None:
    now = time.time()
    with _state_lock:
        ts = _stats["classifier_calls_ts"]
        ts.append(now)
        _prune_deque(ts, now - DAILY_WINDOW_S)
        _stats["classifier_calls_today"] = len(ts)


def _format_classifier_prompt(
    message_text: str,
    recent: list[dict],
    shortnames: list[str],
) -> str:
    ctx_lines = []
    for m in recent[-RECENT_CONTEXT_LIMIT:]:
        author = m.get("author", "?")
        content = (m.get("content") or "").replace("\n", " ")[:200]
        ctx_lines.append(f"{author}: {content}")
    ctx = "\n".join(ctx_lines) if ctx_lines else "(no prior context)"
    emoji_list = ", ".join(shortnames[:60])
    return (
        f"available emoji shortnames: {emoji_list}\n\n"
        f"recent channel context (oldest first):\n{ctx}\n\n"
        f"new message to rate:\n{message_text[:400]}"
    )


async def _classify(
    message_text: str, recent: list[dict], shortnames: list[str]
) -> dict:
    fallback = {"react": False, "emoji": None, "confidence": 0.0, "reason": "fail"}
    text_norm = (message_text or "").strip()
    if not text_norm:
        return fallback

    cache_key = text_norm[:400]
    cached = _classifier_cache_get(cache_key)
    if cached is not None:
        return cached

    user_prompt = _format_classifier_prompt(text_norm, recent, shortnames)
    _bump_classifier_counter()

    try:
        client = _get_client()
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=100,
                system=_CLASSIFIER_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip("` \n")
        data = json.loads(raw)
        decision = {
            "react": bool(data.get("react", False)),
            "emoji": data.get("emoji") or None,
            "confidence": float(data.get("confidence", 0.0) or 0.0),
            "reason": str(data.get("reason", ""))[:200],
        }
        if decision["emoji"] is not None:
            decision["emoji"] = str(decision["emoji"]).strip().lstrip(":").rstrip(":")
        _classifier_cache_put(cache_key, decision)
        _log(
            f"classifier: react={decision['react']} emoji={decision['emoji']!r} "
            f"conf={decision['confidence']:.2f} reason={decision['reason']!r}"
        )
        return decision
    except Exception as e:
        _log(f"classifier error ({type(e).__name__}): {e}")
        return fallback


# ---------------------------------------------------------------------------
# Context gather
# ---------------------------------------------------------------------------
async def _gather_recent_context(
    channel: discord.abc.Messageable, skip_msg_id: Optional[int] = None
) -> list[dict]:
    lines: list[dict] = []
    try:
        async for msg in channel.history(limit=RECENT_CONTEXT_LIMIT + 1):
            if skip_msg_id is not None and msg.id == skip_msg_id:
                continue
            content = (msg.content or "").strip()
            if not content:
                continue
            lines.append({
                "author": getattr(msg.author, "display_name", "someone"),
                "content": content[:300],
            })
            if len(lines) >= RECENT_CONTEXT_LIMIT:
                break
    except Exception as e:
        _log(f"history read error: {type(e).__name__}: {e}")
        return []
    lines.reverse()
    return lines


# ---------------------------------------------------------------------------
# Already-reacted guard
# ---------------------------------------------------------------------------
def _nexus_already_reacted(message: discord.Message) -> bool:
    """Check if Nexus has already dropped any reaction on this message."""
    try:
        bot_id = _bot.user.id if _bot and _bot.user else None
        if bot_id is None:
            return False
        for r in message.reactions:
            if r.me:
                return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public: try_react
# ---------------------------------------------------------------------------
async def try_react(message: discord.Message) -> bool:
    """Hook from on_message. Returns True if a reaction was added."""
    try:
        if message.author.bot:
            return False
        channel = message.channel
        if channel is None:
            return False

        content = (message.content or "").strip()
        if len(content) < MIN_MESSAGE_CHARS:
            return False

        # Consent — cheap bails first
        if _consent_is_quiet():
            return False
        if _consent_is_shy(message.author.id):
            return False
        if _consent_is_opted_out(message.author.id):
            return False

        channel_id = getattr(channel, "id", 0)
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            return False

        if _nexus_already_reacted(message):
            return False

        # Pull context for classifier
        recent = await _gather_recent_context(channel, skip_msg_id=message.id)
        guild = getattr(message, "guild", None)
        shortnames = _available_shortnames(guild)

        decision = await _classify(content, recent, shortnames)
        if not decision.get("react"):
            return False
        if float(decision.get("confidence", 0.0)) < CONFIDENCE_THRESHOLD:
            return False

        emoji_name = decision.get("emoji")
        if not emoji_name:
            return False

        emoji_obj = _resolve_emoji(emoji_name, guild)
        if emoji_obj is None:
            _log(f"unknown emoji {emoji_name!r} — skipping")
            return False

        # Recheck gates after async work
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            return False

        try:
            await message.add_reaction(emoji_obj)
        except discord.Forbidden:
            _log(f"forbidden adding reaction in channel {channel_id}")
            return False
        except discord.HTTPException as e:
            _log(f"reaction http error: {e}")
            return False

        label = (
            emoji_obj if isinstance(emoji_obj, str)
            else f":{getattr(emoji_obj, 'name', '?')}:"
        )
        _record_reaction(channel_id, label)
        _log(
            f"reacted {label} on #{getattr(channel, 'name', channel_id)} "
            f"to {message.author.display_name!r}: {content[:60]!r}"
        )
        return True
    except Exception as e:
        _log(f"try_react fatal ({type(e).__name__}): {e}")
        return False


# ---------------------------------------------------------------------------
# Install + stats
# ---------------------------------------------------------------------------
def install(bot, guild_id: int) -> None:
    global _bot, _guild_id
    _bot = bot
    _guild_id = int(guild_id)
    _log(
        f"installed — channel_cooldown={CHANNEL_COOLDOWN_S}s daily_cap={DAILY_CAP} "
        f"confidence>={CONFIDENCE_THRESHOLD} unicode_emoji={len(UNICODE_EMOJI)}"
    )


def get_stats() -> dict:
    now = time.time()
    with _state_lock:
        _prune_deque(_react_timestamps, now - DAILY_WINDOW_S)
        _prune_deque(_stats["classifier_calls_ts"], now - DAILY_WINDOW_S)
        cooldowns_active: dict[int, float] = {}
        for ch_id, last in _channel_last_react.items():
            remaining = CHANNEL_COOLDOWN_S - (now - last)
            if remaining > 0:
                cooldowns_active[ch_id] = round(remaining, 1)
        return {
            "classifier_calls_today": len(_stats["classifier_calls_ts"]),
            "reactions_today": len(_react_timestamps),
            "reactions_by_emoji": dict(_stats["reactions_by_emoji"]),
            "budget_remaining": max(0, DAILY_CAP - len(_react_timestamps)),
            "cooldowns_active": cooldowns_active,
            "last_react_ts": _stats["last_react_ts"],
            "tunables": {
                "channel_cooldown_s": CHANNEL_COOLDOWN_S,
                "daily_cap": DAILY_CAP,
                "confidence_threshold": CONFIDENCE_THRESHOLD,
                "classifier_model": CLASSIFIER_MODEL,
                "min_message_chars": MIN_MESSAGE_CHARS,
            },
        }


__all__ = [
    "install",
    "try_react",
    "get_stats",
    "CHANNEL_COOLDOWN_S",
    "DAILY_CAP",
    "CONFIDENCE_THRESHOLD",
    "CLASSIFIER_MODEL",
    "MIN_MESSAGE_CHARS",
    "UNICODE_EMOJI",
]
