"""
Nexus quotes — community quote book that fills itself.

Watches TNC messages, asks Haiku if a line is quote-worthy (genuinely funny,
hot take, accidental wisdom, great burn, perfect one-liner), and if so posts
the quote to a #quotes channel with attribution and a jump link back.

Public API:
  install(bot)         — log install line, load state from quotes_state.json
  maybe_quote(message) — classify message; post to #quotes if quote-worthy and
                         cooldowns/caps allow. Returns True if quoted.

Design notes:
  - Pure best-effort. Every haiku call + every post wrapped so errors never
    bubble back into the bot event loop.
  - Classification runs inside asyncio.to_thread (Anthropic SDK is sync).
  - State stored in quotes_state.json with atomic replace + threading.Lock.
  - Entries older than 24h pruned from user_quote_log / server_quote_log on
    every load+save.
  - quoted_msg_ids is capped at 1000 most recent to keep state file bounded.
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
from typing import Optional

import anthropic

import config


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (live-editable constants at module top)
# ---------------------------------------------------------------------------
MIN_CONF: float = 0.8
PER_USER_DAILY_MAX: int = 3
PER_SERVER_DAILY_MAX: int = 10
USER_COOLDOWN_S: int = 600
MIN_LEN: int = 20
MAX_LEN: int = 280

QUOTES_MODEL: str = "claude-haiku-4-5-20251001"
QUOTES_CHANNEL_NAME: str = "quotes"
QUOTED_MSG_IDS_CAP: int = 1000
DAY_SECONDS: int = 24 * 60 * 60

STATE_PATH: Path = Path(config.ROOT) / "quotes_state.json" if hasattr(config, "ROOT") else Path(__file__).parent / "quotes_state.json"
_STATE_LOCK = threading.Lock()

# one-shot warning flag for missing #quotes channel
_warned_no_channel: bool = False


# ---------------------------------------------------------------------------
# Logging helper — match project convention (lowercase, prefixed)
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_quotes] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Anthropic client (lazy, module-level)
# ---------------------------------------------------------------------------
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Channel-name matcher — copied from nexus_eyes._canon to strip leading
# emoji+separator so "quotes", "quotes", "quotes" etc all match.
# ---------------------------------------------------------------------------
_LEAD_STRIP = re.compile(
    r"^[\s_\-\u00a0\u2000-\u206F\u2E00-\u2E7F\u2500-\u257F"
    r"\u2600-\u27BF\U0001F000-\U0001FFFF|\.:,;]+",
    flags=re.UNICODE,
)


def _canon(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = _LEAD_STRIP.sub("", s)
    return s.strip()


# ---------------------------------------------------------------------------
# State file — atomic writes, pruning, threading.Lock guard
# ---------------------------------------------------------------------------
def _default_state() -> dict:
    return {
        "version": 1,
        "quoted_msg_ids": [],
        "user_quote_log": {},     # user_id -> [iso_timestamp, ...]
        "server_quote_log": {},   # guild_id -> [iso_timestamp, ...]
    }


def _prune_state(state: dict) -> dict:
    """Drop user/server log entries older than 24h. Cap quoted_msg_ids."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    cutoff = now_utc - dt.timedelta(seconds=DAY_SECONDS)

    def _keep(iso_str: str) -> bool:
        try:
            t = dt.datetime.fromisoformat(iso_str)
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return t >= cutoff
        except Exception:
            return False

    # user log
    uql = state.get("user_quote_log", {}) or {}
    cleaned_u: dict[str, list[str]] = {}
    for uid, tses in uql.items():
        if not isinstance(tses, list):
            continue
        kept = [t for t in tses if isinstance(t, str) and _keep(t)]
        if kept:
            cleaned_u[str(uid)] = kept
    state["user_quote_log"] = cleaned_u

    # server log
    sql = state.get("server_quote_log", {}) or {}
    cleaned_s: dict[str, list[str]] = {}
    for gid, tses in sql.items():
        if not isinstance(tses, list):
            continue
        kept = [t for t in tses if isinstance(t, str) and _keep(t)]
        if kept:
            cleaned_s[str(gid)] = kept
    state["server_quote_log"] = cleaned_s

    # cap quoted_msg_ids
    ids = state.get("quoted_msg_ids", []) or []
    if not isinstance(ids, list):
        ids = []
    # keep the last N (most recently appended)
    state["quoted_msg_ids"] = [str(x) for x in ids[-QUOTED_MSG_IDS_CAP:]]

    state.setdefault("version", 1)
    return state


def _load_state() -> dict:
    with _STATE_LOCK:
        if not STATE_PATH.exists():
            return _default_state()
        try:
            raw = STATE_PATH.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else _default_state()
        except Exception as e:
            _log(f"state load error ({type(e).__name__}): {e} — starting fresh")
            data = _default_state()
        # back-fill keys
        for k, v in _default_state().items():
            data.setdefault(k, v)
        return _prune_state(data)


def _save_state(state: dict) -> None:
    pruned = _prune_state(state)
    with _STATE_LOCK:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            _log(f"state save error ({type(e).__name__}): {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pre-filters (shape + content)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|<@&\d+>")
_COMMAND_PREFIXES = ("!", "/", "?", ".", "-", "$")


def _is_prefilter_skip(text: str) -> bool:
    """Cheap shape checks before spending a haiku call."""
    t = (text or "").strip()
    if len(t) < MIN_LEN or len(t) > MAX_LEN:
        return True

    # commands
    if t.startswith(_COMMAND_PREFIXES):
        return True

    # strip urls + mentions — if barely anything left, skip
    stripped = _URL_RE.sub("", t)
    stripped = _MENTION_RE.sub("", stripped).strip()
    if len(stripped) < MIN_LEN:
        return True

    return False


# ---------------------------------------------------------------------------
# Haiku classifier
# ---------------------------------------------------------------------------
_CLASSIFIER_SYSTEM = (
    "you're judging if this discord message is quote-worthy for a community "
    "quote book. quote-worthy = genuinely funny, a hot take, an unintentionally "
    "profound moment, a great burn, a perfect one-liner. NOT quote-worthy = "
    "normal banter, agreement, reactions, requests, status updates, anything "
    "boring. respond with json "
    "{\"quote\": bool, \"confidence\": float 0-1, \"reason\": short string}"
)


def _parse_json_loose(raw: str) -> Optional[dict]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip("` \n")
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _classify_sync(text: str, author_name: str) -> dict:
    """Blocking haiku classification call. Returns a dict with quote/confidence/reason."""
    try:
        client = _get_client()
        user_prompt = (
            f"speaker: {author_name}\n"
            f"message:\n{text[:600]}"
        )
        resp = client.messages.create(
            model=QUOTES_MODEL,
            max_tokens=120,
            temperature=0.2,
            system=_CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        data = _parse_json_loose(raw) or {}
        quote = bool(data.get("quote", False))
        try:
            conf = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        reason = str(data.get("reason", "") or "")[:200]
        return {"quote": quote, "confidence": conf, "reason": reason}
    except Exception as e:
        _log(f"classifier error ({type(e).__name__}): {e}")
        return {"quote": False, "confidence": 0.0, "reason": "classifier error"}


# ---------------------------------------------------------------------------
# Cooldown + cap checks
# ---------------------------------------------------------------------------
def _user_cooldown_ok(state: dict, user_id: str, now_utc: dt.datetime) -> bool:
    uql = state.get("user_quote_log", {}) or {}
    tses = uql.get(str(user_id)) or []
    if not tses:
        return True
    latest: Optional[dt.datetime] = None
    for t in tses:
        try:
            parsed = dt.datetime.fromisoformat(t)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            if latest is None or parsed > latest:
                latest = parsed
        except Exception:
            continue
    if latest is None:
        return True
    return (now_utc - latest).total_seconds() >= USER_COOLDOWN_S


def _user_daily_cap_ok(state: dict, user_id: str) -> bool:
    uql = state.get("user_quote_log", {}) or {}
    count = len(uql.get(str(user_id)) or [])
    return count < PER_USER_DAILY_MAX


def _server_daily_cap_ok(state: dict, guild_id: str) -> bool:
    sql = state.get("server_quote_log", {}) or {}
    count = len(sql.get(str(guild_id)) or [])
    return count < PER_SERVER_DAILY_MAX


def _record_quote(state: dict, user_id: str, guild_id: str, msg_id: str, now_utc: dt.datetime) -> None:
    iso = now_utc.isoformat()
    state.setdefault("user_quote_log", {}).setdefault(str(user_id), []).append(iso)
    state.setdefault("server_quote_log", {}).setdefault(str(guild_id), []).append(iso)
    ids = state.setdefault("quoted_msg_ids", [])
    if str(msg_id) not in ids:
        ids.append(str(msg_id))


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------
def _find_quotes_channel(guild) -> Optional[object]:
    """Find the #quotes channel in a guild (case-insensitive, emoji-stripped)."""
    if guild is None:
        return None
    target = _canon(QUOTES_CHANNEL_NAME)
    try:
        text_channels = list(getattr(guild, "text_channels", []) or [])
    except Exception:
        return None
    # exact canon match
    for ch in text_channels:
        try:
            if _canon(getattr(ch, "name", "")) == target:
                return ch
        except Exception:
            continue
    # substring fallback
    for ch in text_channels:
        try:
            if target in _canon(getattr(ch, "name", "")):
                return ch
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_installed = False
_bot = None


def install(bot) -> None:
    """Idempotent. Log install line, prime state file."""
    global _installed, _bot
    _bot = bot
    if _installed:
        return
    _installed = True
    # prime state (creates file if missing, prunes old entries)
    try:
        state = _load_state()
        _save_state(state)
    except Exception as e:
        _log(f"state prime error ({type(e).__name__}): {e}")
    _log(
        f"installed — model={QUOTES_MODEL} min_conf={MIN_CONF} "
        f"per_user_daily={PER_USER_DAILY_MAX} per_server_daily={PER_SERVER_DAILY_MAX} "
        f"user_cooldown={USER_COOLDOWN_S}s len_window={MIN_LEN}-{MAX_LEN}"
    )


async def maybe_quote(message) -> bool:
    """
    Classify message; if quote-worthy and cooldowns/caps allow, post to
    #quotes channel and return True. Otherwise return False. Never raises.
    """
    global _warned_no_channel
    try:
        # Lazy discord import — module shouldn't hard-fail if discord unavail
        try:
            import discord
        except Exception:
            return False

        # Guard: need a real message
        if message is None:
            return False

        # Skip bots
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return False

        # Skip DMs — must have a guild + text channel
        guild = getattr(message, "guild", None)
        if guild is None:
            return False
        channel = getattr(message, "channel", None)
        if channel is None:
            return False
        # Don't quote quotes-channel posts
        try:
            if _canon(getattr(channel, "name", "")) == _canon(QUOTES_CHANNEL_NAME):
                return False
        except Exception:
            pass

        content: str = getattr(message, "content", "") or ""
        if _is_prefilter_skip(content):
            return False

        msg_id = str(getattr(message, "id", ""))
        if not msg_id:
            return False

        user_id = str(getattr(author, "id", ""))
        guild_id = str(getattr(guild, "id", ""))
        if not user_id or not guild_id:
            return False

        # Early state check — dedup + caps before spending a haiku call
        state = _load_state()
        if msg_id in (state.get("quoted_msg_ids") or []):
            return False
        now_utc = dt.datetime.now(dt.timezone.utc)
        if not _user_cooldown_ok(state, user_id, now_utc):
            return False
        if not _user_daily_cap_ok(state, user_id):
            return False
        if not _server_daily_cap_ok(state, guild_id):
            return False

        # Classify with haiku
        try:
            result = await asyncio.to_thread(
                _classify_sync,
                content,
                getattr(author, "display_name", str(author)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"classify thread error ({type(e).__name__}): {e}")
            return False

        conf = float(result.get("confidence", 0.0) or 0.0)
        if not result.get("quote"):
            # Log snippets that got a non-trivial score — useful for tuning MIN_CONF
            if conf >= 0.4:
                _log(f"classify: not-quote conf={conf:.2f} | {content[:80]!r}")
            return False
        if conf < MIN_CONF:
            _log(f"classify: below threshold conf={conf:.2f} (min={MIN_CONF}) | {content[:80]!r}")
            return False
        _log(f"classify: QUOTE conf={conf:.2f} | {content[:80]!r}")

        # Resolve #quotes channel
        quotes_ch = _find_quotes_channel(guild)
        if quotes_ch is None:
            if not _warned_no_channel:
                _warned_no_channel = True
                _log(
                    f"no #{QUOTES_CHANNEL_NAME} channel found in guild "
                    f"{getattr(guild, 'name', '?')} — quotes disabled until created"
                )
            return False

        # Re-check state right before posting (cheap protection against races)
        state = _load_state()
        if msg_id in (state.get("quoted_msg_ids") or []):
            return False
        now_utc = dt.datetime.now(dt.timezone.utc)
        if not _user_cooldown_ok(state, user_id, now_utc):
            return False
        if not _user_daily_cap_ok(state, user_id):
            return False
        if not _server_daily_cap_ok(state, guild_id):
            return False

        # Build embed
        author_name = getattr(author, "display_name", str(author))
        channel_mention = getattr(channel, "mention", f"#{getattr(channel, 'name', '?')}")
        jump_url = getattr(message, "jump_url", "")
        created_at = getattr(message, "created_at", None)

        try:
            embed = discord.Embed(
                description=f"> *\u201c{content}\u201d*",
                url=jump_url or None,
                timestamp=created_at,
            )
            embed.set_footer(
                text=f"\u2014 {author_name} in #{getattr(channel, 'name', '?')}"
            )
        except Exception as e:
            _log(f"embed build error ({type(e).__name__}): {e}")
            return False

        # Send
        try:
            send_fn = getattr(quotes_ch, "send", None)
            if send_fn is None:
                _log(f"#{QUOTES_CHANNEL_NAME} has no send() — can't post")
                return False
            # plain-text fallback line beneath embed so jump link is always clickable
            plain_line = f"\u2014 {author_name}, {channel_mention} \u00b7 [jump]({jump_url})" if jump_url else f"\u2014 {author_name}, {channel_mention}"
            await send_fn(content=plain_line, embed=embed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"send error ({type(e).__name__}): {e}")
            return False

        # Persist
        try:
            _record_quote(state, user_id, guild_id, msg_id, now_utc)
            _save_state(state)
        except Exception as e:
            _log(f"state record error ({type(e).__name__}): {e}")

        _log(
            f"quoted user={author_name}({user_id}) ch=#{getattr(channel, 'name', '?')} "
            f"conf={conf:.2f} reason={result.get('reason', '')!r} msg_id={msg_id}"
        )
        return True

    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"maybe_quote fatal ({type(e).__name__}): {e}")
        return False


__all__ = [
    "install",
    "maybe_quote",
    # tunables for observability / hot-reload
    "MIN_CONF",
    "PER_USER_DAILY_MAX",
    "PER_SERVER_DAILY_MAX",
    "USER_COOLDOWN_S",
    "MIN_LEN",
    "MAX_LEN",
    "QUOTES_MODEL",
    "QUOTES_CHANNEL_NAME",
]
