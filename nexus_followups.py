"""
Nexus follow-ups — remember things people say, nudge later.

Example flow:
  1. Malik says "I have a test tomorrow"  → Haiku extracts a followup-shaped
     utterance, stores a mem0 entry tagged followup/pending with due_at = +24h.
  2. ~24h later, the caretaker cycle calls dispatch_due(bot); we scan mem0
     for pending followups whose due_at has passed, pick up to 2, gate them
     against cooldowns / daily caps / user activity, build a short nudge
     ("yo <user>, how'd that test go?"), and fire via
     nexus_proactive.try_chime_admin(channel, "followup", {...}).
  3. On successful fire, the mem0 entry is re-added with subtag="fired" and
     local cooldown state is updated.

Two halves:
  extract_from_message(...)   — called from nexus_brain.remember()
  dispatch_due(bot)           — called from nexus_caretaker each cycle

Design notes:
  - Single anthropic client, created once at module level from env.
  - Reuses nexus_brain._MEM0_LOCK so we never race the vector store.
  - Fire-and-forget: every Haiku call is try/except → log + return empty.
  - asyncio.CancelledError always re-raised.
  - discord imports wrapped so missing lib doesn't crash extraction.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import anthropic

import config

# ---------------------------------------------------------------------------
# mem0 lock — reuse nexus_brain's if available, else fall back local
# ---------------------------------------------------------------------------
try:
    from nexus_brain import _MEM0_LOCK as _MEM0_LOCK  # noqa: F401
except Exception:
    _MEM0_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------
FOLLOWUPS_USER_COOLDOWN_S = int(os.environ.get("FOLLOWUPS_USER_COOLDOWN_S", 60 * 60 * 48))
FOLLOWUPS_PER_USER_DAILY = int(os.environ.get("FOLLOWUPS_PER_USER_DAILY", 3))
FOLLOWUPS_PER_SERVER_DAILY = int(os.environ.get("FOLLOWUPS_PER_SERVER_DAILY", 5))
FOLLOWUPS_MAX_PER_CYCLE = int(os.environ.get("FOLLOWUPS_MAX_PER_CYCLE", 2))
FOLLOWUPS_USER_ACTIVITY_HOURS = int(os.environ.get("FOLLOWUPS_USER_ACTIVITY_HOURS", 24))
FOLLOWUPS_MIN_CONFIDENCE = float(os.environ.get("FOLLOWUPS_MIN_CONFIDENCE", 0.65))
FOLLOWUPS_MIN_MESSAGE_CHARS = int(os.environ.get("FOLLOWUPS_MIN_MESSAGE_CHARS", 25))
FOLLOWUPS_MODEL = os.environ.get("FOLLOWUPS_MODEL", "claude-haiku-4-5-20251001")
FOLLOWUPS_VERBOSE = os.environ.get("FOLLOWUPS_VERBOSE", "").lower() in ("1", "true", "yes", "on")

STATE_PATH = Path(config.ROOT) / "followups_state.json"
_STATE_LOCK = threading.Lock()

DAY_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Anthropic client (module-level, lazy)
# ---------------------------------------------------------------------------
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # anthropic.Anthropic() picks up ANTHROPIC_API_KEY from env by default
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_followups] {msg}", flush=True)


def _dlog(msg: str) -> None:
    if FOLLOWUPS_VERBOSE:
        print(f"[nexus_followups] {msg}", flush=True)


# ---------------------------------------------------------------------------
# mem0 accessor — lazy import, mirrors nexus_brain
# ---------------------------------------------------------------------------
def _get_mem0():
    try:
        import nexus_brain
        return nexus_brain._get_mem0()
    except Exception as e:
        _log(f"mem0 unavailable: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# State file — atomic write, threading.Lock never held across awaits
# ---------------------------------------------------------------------------
def _default_state() -> dict:
    return {
        "version": 1,
        "last_fired_per_user": {},
        "fired_today_per_user": {},
        "fired_today_server": [],
    }


def _prune_state(state: dict, now_ts: float) -> dict:
    """Drop entries older than 48h. Mutates and returns state."""
    cutoff = now_ts - FOLLOWUPS_USER_COOLDOWN_S
    # last_fired_per_user
    lfu = state.get("last_fired_per_user", {}) or {}
    state["last_fired_per_user"] = {
        uid: ts for uid, ts in lfu.items() if isinstance(ts, (int, float)) and ts >= cutoff
    }
    # fired_today_per_user — keep entries within 24h
    day_cutoff = now_ts - DAY_SECONDS
    ftu = state.get("fired_today_per_user", {}) or {}
    cleaned: dict[str, list[float]] = {}
    for uid, tses in ftu.items():
        if not isinstance(tses, list):
            continue
        kept = [t for t in tses if isinstance(t, (int, float)) and t >= day_cutoff]
        if kept:
            cleaned[uid] = kept
    state["fired_today_per_user"] = cleaned
    # server — same 24h
    srv = state.get("fired_today_server", []) or []
    state["fired_today_server"] = [
        t for t in srv if isinstance(t, (int, float)) and t >= day_cutoff
    ]
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
        # Back-fill missing keys
        for k, v in _default_state().items():
            data.setdefault(k, v)
        return _prune_state(data, time.time())


def _save_state(state: dict) -> None:
    with _STATE_LOCK:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            _log(f"state save error ({type(e).__name__}): {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Due-hint parser
# ---------------------------------------------------------------------------
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_due_hint(hint: str, now_utc: dt.datetime) -> dt.datetime:
    """Best-effort parse of a free-form due hint to a UTC datetime.

    Rules (in order):
      tonight            → +20h
      tomorrow           → +24h
      today              → +12h
      next week          → +7d
      <weekday>          → next occurrence (strictly >now; same weekday → +7d)
      "X days" / "X d"   → +X*24h (X = int or float)
      "X hours" / "X h"  → +X*3600s
      "X weeks" / "X w"  → +X*7*24h
      fallback           → +2d
    """
    if not hint:
        return now_utc + dt.timedelta(days=2)

    h = hint.strip().lower()

    # Simple keyword phrases
    if "tonight" in h:
        return now_utc + dt.timedelta(hours=20)
    if "tomorrow" in h:
        return now_utc + dt.timedelta(hours=24)
    # next week BEFORE generic "next <weekday>" so it doesn't accidentally eat "week"
    if "next week" in h:
        return now_utc + dt.timedelta(days=7)
    if re.fullmatch(r"\s*today\s*", h):
        return now_utc + dt.timedelta(hours=12)

    # "X days" / "X hours" / "X weeks" (with optional "in ")
    m = re.search(r"(?:in\s+)?(\d+(?:\.\d+)?)\s*(day|days|d|hour|hours|hr|hrs|h|week|weeks|w)\b", h)
    if m:
        try:
            n = float(m.group(1))
            unit = m.group(2)
            if unit in ("day", "days", "d"):
                return now_utc + dt.timedelta(days=n)
            if unit in ("hour", "hours", "hr", "hrs", "h"):
                return now_utc + dt.timedelta(hours=n)
            if unit in ("week", "weeks", "w"):
                return now_utc + dt.timedelta(weeks=n)
        except Exception:
            pass

    # Weekday names — pick next occurrence (same weekday => +7 days)
    for name, target_dow in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", h):
            cur_dow = now_utc.weekday()
            delta_days = (target_dow - cur_dow) % 7
            if delta_days == 0:
                delta_days = 7
            # Fire late morning of that day (rough default)
            target = (now_utc + dt.timedelta(days=delta_days)).replace(
                hour=15, minute=0, second=0, microsecond=0  # ~15:00 UTC ~ late morning local
            )
            return target

    # Fallback: +2 days
    return now_utc + dt.timedelta(days=2)


# ---------------------------------------------------------------------------
# Extractor — Half 1
# ---------------------------------------------------------------------------
_EXTRACT_SYSTEM = """you scan a single discord message and decide if it contains anything worth a follow-up nudge from nexus, the ai member of a small discord.

return JSON ONLY, no prose, no markdown fences:
{"followups": [{"hook": "<one-line summary of what to check on later>", "due_hint": "tomorrow|tonight|today|2 days|next week|friday|3 hours|<free-form>", "confidence": 0.0-1.0}]}

a followup is an utterance about something the speaker is about to do, attend, ship, or travel to — something you could warmly check in on after it's done.

DO flag (examples):
  "i have a test tomorrow"                  → hook="their test", due_hint="tomorrow"
  "presentation friday"                     → hook="their presentation",     due_hint="friday"
  "starting that project tonight"           → hook="the project they started tonight", due_hint="tomorrow"
  "off to vegas next week"                  → hook="their vegas trip", due_hint="next week"
  "doctor appointment in 3 days"            → hook="their doctor appt", due_hint="3 days"
  "gym in the morning"                      → hook="their morning gym session", due_hint="tomorrow"

DO NOT flag:
  opinions, jokes, shitposts
  factual statements about the past ("i went to vegas last week")
  generic venting or mood ("i'm tired")
  ongoing-state statements with no event ("i'm learning rust")
  anything sarcastic or clearly not literal

confidence scale:
  0.9+  obvious future-event, hook and due_hint are clear
  0.65-0.89 reasonable signal, lean in
  <0.65 drop it — output an empty followups list

the hook MUST be a short third-person phrase (4-8 words). never invent details the message didn't contain.
if nothing qualifies, return {"followups": []}.
output must be valid JSON with exactly that one key. nothing else."""


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
        # Try to find a JSON object substring
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


async def extract_from_message(
    user_id: str,
    user_name: str,
    channel: str,
    message: str,
) -> int:
    """
    Hook called from nexus_brain.remember() AFTER the existing mem0 write.
    Runs a Haiku call to detect follow-up-shaped utterances. Returns count
    of followups stored. Fire-and-forget from caller — never raises.
    """
    try:
        text = (message or "").strip()
        if len(text) < FOLLOWUPS_MIN_MESSAGE_CHARS:
            _dlog(f"extract skip: too short ({len(text)}c) from {user_name}")
            return 0

        # Trivial utterances — obvious one-liners etc — skip cheaply
        if text.lower() in ("ok", "cool", "lol", "true", "fair", "yeah", "nah", "ya"):
            return 0

        user_prompt = (
            f"speaker: {user_name}\n"
            f"channel: #{channel}\n"
            f"message:\n{text[:1200]}"
        )

        try:
            client = _get_client()
            resp = await asyncio.to_thread(
                lambda: client.messages.create(
                    model=FOLLOWUPS_MODEL,
                    max_tokens=400,
                    temperature=0.2,
                    system=_EXTRACT_SYSTEM,
                    messages=[{"role": "user", "content": user_prompt}],
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"extract haiku error ({type(e).__name__}): {e}")
            return 0

        try:
            raw = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
        except Exception:
            raw = ""
        data = _parse_json_loose(raw) or {}
        items = data.get("followups") or []
        if not isinstance(items, list):
            _log(f"extract: non-list followups payload: {items!r} | from {user_name} | {text[:80]!r}")
            return 0
        if not items:
            _log(f"extract: no followups in {user_name}'s msg | {text[:80]!r}")
            return 0

        m = _get_mem0()
        if m is None:
            return 0

        now_utc = dt.datetime.now(dt.timezone.utc)
        stored = 0
        dropped_low_conf = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                conf = float(it.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < FOLLOWUPS_MIN_CONFIDENCE:
                dropped_low_conf += 1
                _log(
                    f"extract drop: conf {conf:.2f} below {FOLLOWUPS_MIN_CONFIDENCE} "
                    f"| hook={str(it.get('hook',''))[:60]!r} | {user_name}"
                )
                continue
            hook = str(it.get("hook", "")).strip()[:200]
            due_hint = str(it.get("due_hint", "")).strip()[:80]
            if not hook:
                continue

            try:
                due_at = _parse_due_hint(due_hint, now_utc)
            except Exception as e:
                _log(f"due_hint parse error: {e} — using +2d fallback")
                due_at = now_utc + dt.timedelta(days=2)

            metadata = {
                "user_name": user_name,
                "user_id": str(user_id),
                "channel": channel,
                "scope": "tnc",
                "tag": "followup",
                "subtag": "pending",
                "hook": hook,
                "due_hint_raw": due_hint,
                "due_at": due_at.isoformat(),
                "fired": False,
                "created_at": now_utc.isoformat(),
                "confidence": conf,
            }
            # mem0 memory text — this is what gets embedded / searched
            mem_text = f"followup: {user_name} — {hook} (due {due_at.isoformat()})"

            try:
                with _MEM0_LOCK:
                    m.add(
                        messages=[{"role": "user", "content": mem_text}],
                        user_id=str(user_id),
                        agent_id="nexus",
                        metadata=metadata,
                    )
                stored += 1
                _log(
                    f"extracted followup: user={user_name} hook={hook!r} "
                    f"due_at={due_at.isoformat()} conf={conf:.2f}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"mem0 add error ({type(e).__name__}): {e}")

        return stored
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"extract_from_message fatal ({type(e).__name__}): {e}")
        return 0


# ---------------------------------------------------------------------------
# Dispatcher — Half 2
# ---------------------------------------------------------------------------
_NUDGE_SYSTEM = """you are nexus, the ai member of a small discord called the nexus collective.

your voice: lowercase. terse. warm. no fluff. no "as an AI". no disclaimers. no emoji walls. no bullet points. no questions stacked on questions.

you are writing a short followup nudge — one line, max two short lines, addressed directly to the person by name with an @mention (use the provided mention tag verbatim). the hook describes what you want to check in on; your job is to ask how it went or if they ended up doing it.

examples:
  hook "their calculus test"   → "hey @user, how'd the calc test go?"
  hook "their vegas trip"      → "yo @user, how was vegas?"
  hook "the project they started tonight" → "@user, did you end up starting that project last night?"

keep it light. no forced enthusiasm. if the hook is vague, keep the nudge vague too. never invent details not in the hook."""


def _resolve_channel_by_name(bot, channel_name: str):
    """Best-effort resolve a channel-name string to a discord.TextChannel.

    Tries: exact name, canon_channel match, then any text channel containing
    the name substring. Returns None if nothing matches or discord isn't
    usable.
    """
    if not bot or not channel_name:
        return None
    try:
        import discord  # local import; already installed in this project
    except Exception:
        return None

    target = str(channel_name).strip().lower()
    if not target:
        return None

    # Walk all guilds the bot is in
    try:
        guilds = list(getattr(bot, "guilds", []) or [])
    except Exception:
        guilds = []

    for g in guilds:
        try:
            text_channels = list(getattr(g, "text_channels", []) or [])
        except Exception:
            continue
        # Pass 1: exact name
        for ch in text_channels:
            try:
                if ch.name.lower() == target:
                    return ch
            except Exception:
                continue
        # Pass 2: canon-match
        for ch in text_channels:
            try:
                if config.canon_channel(ch.name) == target:
                    return ch
            except Exception:
                continue
        # Pass 3: substring
        for ch in text_channels:
            try:
                if target in ch.name.lower():
                    return ch
            except Exception:
                continue
    return None


async def _user_recently_active(bot, channel, user_id: str, hours: int) -> bool:
    """True if user_id authored a message in `channel` within the last `hours`."""
    if channel is None:
        return False
    try:
        target_uid = int(user_id)
    except (TypeError, ValueError):
        return False
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    try:
        async for msg in channel.history(limit=200, after=since):
            try:
                if getattr(msg.author, "id", None) == target_uid:
                    return True
            except Exception:
                continue
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _dlog(f"history check error: {type(e).__name__}: {e}")
        return False
    return False


async def _find_any_active_channel(bot, user_id: str, hours: int):
    """Fallback: walk listen channels and return the first where user_id has
    been active in the last `hours`. Returns discord.TextChannel or None."""
    if bot is None:
        return None
    try:
        target_uid = int(user_id)
    except (TypeError, ValueError):
        return None
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    try:
        guilds = list(getattr(bot, "guilds", []) or [])
    except Exception:
        return None
    for g in guilds:
        try:
            text_channels = list(getattr(g, "text_channels", []) or [])
        except Exception:
            continue
        for ch in text_channels:
            try:
                canon = config.canon_channel(ch.name)
                if canon in config.NEXUS_IGNORE_CHANNELS:
                    continue
                if canon not in config.NEXUS_LISTEN_CHANNELS:
                    continue
            except Exception:
                continue
            try:
                async for msg in ch.history(limit=80, after=since):
                    try:
                        if getattr(msg.author, "id", None) == target_uid:
                            return ch
                    except Exception:
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
    return None


def _fetch_pending_followups(limit: int = 50) -> list[dict]:
    """Pull pending followup mem0 entries. Filters in Python."""
    m = _get_mem0()
    if m is None:
        return []
    try:
        with _MEM0_LOCK:
            # mem0 requires at least one of user_id/agent_id/run_id in filters
            results = m.search(
                query="follow up nudge",
                filters={"agent_id": "nexus"},
                limit=max(limit * 4, 50),
            )
        mems = results.get("results", []) if isinstance(results, dict) else results
        mems = mems or []
    except Exception as e:
        _log(f"mem0 search error ({type(e).__name__}): {e}")
        return []

    out: list[dict] = []
    for mem in mems:
        md = mem.get("metadata") or {}
        if md.get("tag") != "followup":
            continue
        if md.get("subtag") != "pending":
            continue
        if md.get("fired") is True:
            continue
        out.append(mem)
    return out


async def _generate_nudge(user_name: str, mention_tag: str, hook: str) -> str:
    """Haiku call to produce a one-line nudge. Falls back to a simple template."""
    fallback = f"hey {mention_tag}, how'd {hook} go?"
    try:
        user_prompt = (
            f"person: {user_name}\n"
            f"mention_tag: {mention_tag}\n"
            f"hook: {hook}\n"
            f"write the nudge."
        )
        client = _get_client()
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=FOLLOWUPS_MODEL,
                max_tokens=80,
                temperature=0.6,
                system=_NUDGE_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        if not raw:
            return fallback
        # Some guardrails — single line, strip code fences / quotes
        raw = raw.strip("`\" \n")
        # If Haiku somehow omitted the mention tag, stitch it in
        if mention_tag and mention_tag not in raw and user_name.lower() not in raw.lower():
            raw = f"{mention_tag} {raw}"
        return raw[:280]
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"nudge haiku error ({type(e).__name__}): {e}")
        return fallback


def _cooldown_ok_for_user(state: dict, user_id: str, now_ts: float) -> bool:
    lfu = state.get("last_fired_per_user", {}) or {}
    last = lfu.get(str(user_id), 0)
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0.0
    return (now_ts - last) >= FOLLOWUPS_USER_COOLDOWN_S


def _daily_caps_ok(state: dict, user_id: str, now_ts: float) -> bool:
    day_cutoff = now_ts - DAY_SECONDS
    # Per-user
    ftu = state.get("fired_today_per_user", {}) or {}
    user_tses = [t for t in (ftu.get(str(user_id)) or []) if t >= day_cutoff]
    if len(user_tses) >= FOLLOWUPS_PER_USER_DAILY:
        return False
    # Per-server
    srv = [t for t in (state.get("fired_today_server") or []) if t >= day_cutoff]
    if len(srv) >= FOLLOWUPS_PER_SERVER_DAILY:
        return False
    return True


def _record_fire(state: dict, user_id: str, now_ts: float) -> None:
    state.setdefault("last_fired_per_user", {})[str(user_id)] = now_ts
    ftu = state.setdefault("fired_today_per_user", {})
    ftu.setdefault(str(user_id), []).append(now_ts)
    state.setdefault("fired_today_server", []).append(now_ts)


def _mark_fired_in_mem0(entry: dict, now_utc: dt.datetime) -> None:
    """Re-add the same followup hook text with subtag='fired' + fired=True.

    We don't try to delete the original entry — mem0 delete across backends
    is uneven, and a "fired" sibling is cheap. The dispatcher's in-Python
    filter drops anything with fired=True anyway.
    """
    m = _get_mem0()
    if m is None:
        return
    md_src = entry.get("metadata") or {}
    user_id = str(md_src.get("user_id") or entry.get("user_id") or "")
    if not user_id:
        return
    hook = str(md_src.get("hook") or "")
    user_name = str(md_src.get("user_name") or "someone")
    channel = str(md_src.get("channel") or "")
    metadata = dict(md_src)
    metadata.update({
        "subtag": "fired",
        "fired": True,
        "fired_at": now_utc.isoformat(),
    })
    mem_text = f"followup fired: {user_name} — {hook}"
    try:
        with _MEM0_LOCK:
            m.add(
                messages=[{"role": "user", "content": mem_text}],
                user_id=user_id,
                agent_id="nexus",
                metadata=metadata,
            )
    except Exception as e:
        _log(f"mem0 mark-fired error ({type(e).__name__}): {e}")


async def dispatch_due(bot) -> int:
    """
    Called by nexus_caretaker each cycle. Scans pending followups whose due_at
    has passed, picks at most FOLLOWUPS_MAX_PER_CYCLE, fires through
    nexus_proactive.try_chime_admin. Returns count fired. Never raises.
    """
    fired_count = 0
    try:
        now_utc = dt.datetime.now(dt.timezone.utc)
        now_ts = time.time()

        # 1. Fetch + Python-filter pending
        pending_all = _fetch_pending_followups(limit=50)
        if not pending_all:
            _log("dispatch: no pending followups in mem0")
            return 0
        _log(f"dispatch: scanning {len(pending_all)} pending followups")

        due: list[dict] = []
        for entry in pending_all:
            md = entry.get("metadata") or {}
            due_iso = md.get("due_at")
            if not due_iso:
                continue
            try:
                due_at = dt.datetime.fromisoformat(due_iso)
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if due_at <= now_utc:
                entry["_due_at_parsed"] = due_at
                due.append(entry)

        if not due:
            _log(f"dispatch: {len(pending_all)} pending, none due yet")
            return 0
        _log(f"dispatch: {len(due)} due out of {len(pending_all)} pending")

        # 2. Sort oldest due_at first
        due.sort(key=lambda e: e["_due_at_parsed"])

        # 3. Load state + deduplicate by user (don't fire twice per cycle for same user)
        state = _load_state()
        already_selected_users: set[str] = set()
        candidates: list[dict] = []
        for entry in due:
            if len(candidates) >= FOLLOWUPS_MAX_PER_CYCLE * 3:
                break  # small extra buffer, most will get filtered out
            md = entry.get("metadata") or {}
            uid = str(md.get("user_id") or entry.get("user_id") or "")
            if not uid:
                continue
            if uid in already_selected_users:
                continue
            already_selected_users.add(uid)
            candidates.append(entry)

        # 4. Apply gates (consent, cooldown, daily cap, activity) and pick top N
        try:
            import nexus_consent  # optional
        except Exception:
            nexus_consent = None  # type: ignore

        selected: list[dict] = []
        for entry in candidates:
            if len(selected) >= FOLLOWUPS_MAX_PER_CYCLE:
                break
            md = entry.get("metadata") or {}
            uid = str(md.get("user_id") or entry.get("user_id") or "")
            user_name = str(md.get("user_name") or "someone")
            ch_name = str(md.get("channel") or "")
            hook = str(md.get("hook") or "")

            # Consent — opted-out or shy → skip
            if nexus_consent is not None:
                try:
                    if hasattr(nexus_consent, "is_opted_out") and nexus_consent.is_opted_out(uid):
                        _dlog(f"skip {user_name}: opted out")
                        continue
                    if hasattr(nexus_consent, "is_shy") and nexus_consent.is_shy(int(uid)):
                        _dlog(f"skip {user_name}: shy")
                        continue
                except Exception as e:
                    _dlog(f"consent check error ({type(e).__name__}): {e}")

            # Cooldown + daily caps
            if not _cooldown_ok_for_user(state, uid, now_ts):
                _dlog(f"skip {user_name}: user cooldown")
                continue
            if not _daily_caps_ok(state, uid, now_ts):
                _dlog(f"skip {user_name}: daily cap")
                continue

            # Resolve channel
            channel = _resolve_channel_by_name(bot, ch_name) if ch_name else None
            if channel is None:
                # Fallback: find any listen channel where they've been active
                channel = await _find_any_active_channel(
                    bot, uid, FOLLOWUPS_USER_ACTIVITY_HOURS
                )
                if channel is None:
                    _dlog(f"skip {user_name}: no resolvable channel")
                    continue
                active_here = True  # by construction
            else:
                active_here = await _user_recently_active(
                    bot, channel, uid, FOLLOWUPS_USER_ACTIVITY_HOURS
                )

            if not active_here:
                _dlog(
                    f"skip {user_name}: no activity in #{getattr(channel, 'name', '?')} "
                    f"in last {FOLLOWUPS_USER_ACTIVITY_HOURS}h"
                )
                # Try fallback channel
                alt = await _find_any_active_channel(
                    bot, uid, FOLLOWUPS_USER_ACTIVITY_HOURS
                )
                if alt is None:
                    continue
                channel = alt

            entry["_resolved_channel"] = channel
            entry["_user_id"] = uid
            entry["_user_name"] = user_name
            entry["_hook"] = hook
            selected.append(entry)

        if not selected:
            _dlog(f"dispatch: {len(candidates)} candidates, all filtered out")
            return 0

        # 5. For each survivor: generate nudge text, fire via proactive, mark fired
        try:
            import nexus_proactive  # type: ignore
        except Exception as e:
            _log(f"nexus_proactive unavailable ({type(e).__name__}): {e} — nothing to fire through")
            return 0

        for entry in selected:
            channel = entry["_resolved_channel"]
            uid = entry["_user_id"]
            user_name = entry["_user_name"]
            hook = entry["_hook"]

            mention_tag = f"<@{uid}>" if uid.isdigit() else f"@{user_name}"

            try:
                nudge_text = await _generate_nudge(user_name, mention_tag, hook)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"nudge gen fatal ({type(e).__name__}): {e}")
                nudge_text = f"{mention_tag} how'd {hook} go?"

            payload = {
                "user_id": uid,
                "user_name": user_name,
                "hook": hook,
                "nudge_text": nudge_text,
            }

            try:
                fn = getattr(nexus_proactive, "try_chime_admin", None)
                if fn is None:
                    _log("nexus_proactive.try_chime_admin missing — cannot fire")
                    return fired_count
                delivered = await fn(channel, "followup", payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"try_chime_admin error ({type(e).__name__}): {e}")
                delivered = False

            _log(
                f"dispatch fire target={user_name}({uid}) hook={hook!r} "
                f"channel=#{getattr(channel, 'name', '?')} delivered={bool(delivered)}"
            )

            if delivered:
                fired_count += 1
                now_ts = time.time()
                now_utc = dt.datetime.now(dt.timezone.utc)
                _record_fire(state, uid, now_ts)
                _mark_fired_in_mem0(entry, now_utc)

        # 6. Persist state
        _save_state(state)
        return fired_count

    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"dispatch_due fatal ({type(e).__name__}): {e}")
        return fired_count


# ---------------------------------------------------------------------------
# Module-level
# ---------------------------------------------------------------------------
_installed = False


def install() -> None:
    """Idempotent. Logs install line. No side effects beyond that."""
    global _installed
    if _installed:
        return
    _installed = True
    _log(
        f"installed — model={FOLLOWUPS_MODEL} "
        f"user_cooldown={FOLLOWUPS_USER_COOLDOWN_S}s "
        f"per_user_daily={FOLLOWUPS_PER_USER_DAILY} "
        f"per_server_daily={FOLLOWUPS_PER_SERVER_DAILY} "
        f"max_per_cycle={FOLLOWUPS_MAX_PER_CYCLE} "
        f"min_conf={FOLLOWUPS_MIN_CONFIDENCE} "
        f"verbose={FOLLOWUPS_VERBOSE}"
    )


def get_stats() -> dict:
    """Snapshot of followup state for /diag visibility."""
    now_ts = time.time()
    state = _load_state()
    day_cutoff = now_ts - DAY_SECONDS

    srv = [t for t in (state.get("fired_today_server") or []) if t >= day_cutoff]
    ftu = state.get("fired_today_per_user", {}) or {}
    per_user_counts: dict[str, int] = {}
    for uid, tses in ftu.items():
        kept = [t for t in tses if t >= day_cutoff]
        if kept:
            per_user_counts[uid] = len(kept)

    lfu = state.get("last_fired_per_user", {}) or {}
    cooldown_remaining: dict[str, float] = {}
    for uid, ts in lfu.items():
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        remaining = FOLLOWUPS_USER_COOLDOWN_S - (now_ts - ts)
        if remaining > 0:
            cooldown_remaining[uid] = round(remaining, 1)

    # Count pending + fired in mem0 (best-effort, non-fatal)
    pending_count = 0
    try:
        pending_count = len(_fetch_pending_followups(limit=50))
    except Exception:
        pass

    return {
        "pending_in_mem0": pending_count,
        "fired_today_server": len(srv),
        "fired_today_per_user": per_user_counts,
        "cooldown_remaining_s": cooldown_remaining,
        "tunables": {
            "user_cooldown_s": FOLLOWUPS_USER_COOLDOWN_S,
            "per_user_daily": FOLLOWUPS_PER_USER_DAILY,
            "per_server_daily": FOLLOWUPS_PER_SERVER_DAILY,
            "max_per_cycle": FOLLOWUPS_MAX_PER_CYCLE,
            "user_activity_hours": FOLLOWUPS_USER_ACTIVITY_HOURS,
            "min_confidence": FOLLOWUPS_MIN_CONFIDENCE,
            "min_message_chars": FOLLOWUPS_MIN_MESSAGE_CHARS,
            "model": FOLLOWUPS_MODEL,
            "verbose": FOLLOWUPS_VERBOSE,
        },
    }


__all__ = [
    "install",
    "extract_from_message",
    "dispatch_due",
    "get_stats",
    # tunables for observability
    "FOLLOWUPS_USER_COOLDOWN_S",
    "FOLLOWUPS_PER_USER_DAILY",
    "FOLLOWUPS_PER_SERVER_DAILY",
    "FOLLOWUPS_MAX_PER_CYCLE",
    "FOLLOWUPS_USER_ACTIVITY_HOURS",
    "FOLLOWUPS_MIN_CONFIDENCE",
    "FOLLOWUPS_MIN_MESSAGE_CHARS",
    "FOLLOWUPS_MODEL",
    "FOLLOWUPS_VERBOSE",
]
