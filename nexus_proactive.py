"""
Nexus text proactivity layer.

Lets Nexus chime in unprompted when it actually has something useful to add —
an unanswered question it remembers context for, a thread callback across
users, visible distress needing a calmer voice, a new-member orient moment,
or a genuine celebration. The bar is HIGH by design: proactive chatter is
more annoying than silence.

Pipeline for each non-triggered text message:
    classifier gate -> cooldown gate -> budget gate -> consent gate -> reply

Cost: one Haiku classifier call per non-trigger message (with 60s identical-
text cache for bursts). Reply generation reuses nexus_brain.reply() exactly so
persona/voice/memory stay consistent.

Public API (see module bottom):
    install(bot, guild_id)
    await try_chime_text(message)
    await try_chime_voice(voice_channel, recent_lines)
    await try_chime_admin(channel, kind, payload)
    get_stats()
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Any, Optional

import discord

import config
import nexus_brain

# nexus_consent helpers may or may not exist yet — Agent C is adding them.
# We wrap every call in try/except AttributeError and treat as False.
try:
    import nexus_consent
except Exception:  # pragma: no cover — consent module should exist, but fail soft
    nexus_consent = None  # type: ignore[assignment]

try:
    import nexus_voice
except Exception:
    nexus_voice = None  # type: ignore[assignment]

from anthropic import Anthropic


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
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


# Per-channel cooldown between unprompted chimes
CHANNEL_COOLDOWN_S = _env_int("NEXUS_PROACTIVE_CHANNEL_COOLDOWN_S", 10 * 60)

# Server-wide rolling 24h cap
DAILY_CAP = _env_int("NEXUS_PROACTIVE_DAILY_CAP", 20)

# Per-kind sub-cooldowns (seconds)
WEEKLY_DIGEST_COOLDOWN_S = _env_int(
    "NEXUS_PROACTIVE_WEEKLY_DIGEST_COOLDOWN_S", 6 * 24 * 60 * 60
)
DEAD_CHANNEL_COOLDOWN_S = _env_int(
    "NEXUS_PROACTIVE_DEAD_CHANNEL_COOLDOWN_S", 24 * 60 * 60
)

# Classifier confidence threshold
CONFIDENCE_THRESHOLD = _env_float("NEXUS_PROACTIVE_CONFIDENCE", 0.7)

# Identical-text classifier cache duration (seconds)
CLASSIFIER_CACHE_TTL_S = 60

# How much recent channel context to pull for the classifier + reply
RECENT_CONTEXT_LIMIT = 12

# Rolling window size for the daily cap (seconds)
DAILY_WINDOW_S = 24 * 60 * 60

# Haiku model for the classifier gate
CLASSIFIER_MODEL = os.environ.get(
    "NEXUS_PROACTIVE_MODEL", "claude-haiku-4-5-20251001"
)

# Min message length to even consider classifying (one-line acks / emoji noise skip)
MIN_MESSAGE_CHARS = 12


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_bot: Optional[discord.Client] = None
_guild_id: Optional[int] = None

_state_lock = threading.Lock()

# Per-channel: {channel_id: last_chime_ts}
_channel_last_chime: dict[int, float] = {}

# Per (kind, channel_id): last ts — for admin kinds like dead_channel per channel
_kind_channel_last: dict[tuple[str, int], float] = {}

# Per kind: last ts server-wide (for weekly_digest etc)
_kind_last: dict[str, float] = {}

# Rolling deque of chime timestamps (for daily cap)
_chime_timestamps: deque[float] = deque()

# Stats
_stats = {
    "classifier_calls_today": 0,
    "chimes_today": 0,
    "chimes_by_kind": {},
    "last_chime_ts": 0.0,
    "classifier_calls_ts": deque(),  # rolling for today counter
}

# Classifier burst cache: {normalized_text: (expiry_ts, decision_dict)}
_classifier_cache: dict[str, tuple[float, dict]] = {}

# Anthropic client for the classifier
_client: Optional[Anthropic] = None


def _log(msg: str) -> None:
    print(f"[nexus_proactive] {msg}", flush=True)


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# Consent stubs — Agent C is adding is_quiet / is_shy. Until then, always False.
# ---------------------------------------------------------------------------
def _consent_is_quiet() -> bool:
    if nexus_consent is None:
        return False
    try:
        fn = getattr(nexus_consent, "is_quiet", None)
        if fn is None:
            return False
        return bool(fn())
    except AttributeError:
        return False
    except Exception as e:
        _log(f"is_quiet error (treating as not-quiet): {type(e).__name__}: {e}")
        return False


def _consent_is_shy(user_id: int) -> bool:
    if nexus_consent is None:
        return False
    try:
        fn = getattr(nexus_consent, "is_shy", None)
        if fn is None:
            return False
        return bool(fn(user_id))
    except AttributeError:
        return False
    except Exception as e:
        _log(f"is_shy error (treating as not-shy): {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Budget + cooldown gates
# ---------------------------------------------------------------------------
def _prune_deque(dq: deque, cutoff: float) -> None:
    while dq and dq[0] < cutoff:
        dq.popleft()


def _budget_available() -> bool:
    now = time.time()
    with _state_lock:
        _prune_deque(_chime_timestamps, now - DAILY_WINDOW_S)
        return len(_chime_timestamps) < DAILY_CAP


def _channel_cooldown_ok(channel_id: int, cooldown_s: int = CHANNEL_COOLDOWN_S) -> bool:
    now = time.time()
    with _state_lock:
        last = _channel_last_chime.get(channel_id, 0.0)
        return (now - last) >= cooldown_s


def _kind_cooldown_ok(kind: str, channel_id: Optional[int] = None) -> bool:
    now = time.time()
    with _state_lock:
        if kind == "weekly_digest":
            last = _kind_last.get(kind, 0.0)
            return (now - last) >= WEEKLY_DIGEST_COOLDOWN_S
        if kind == "dead_channel" and channel_id is not None:
            last = _kind_channel_last.get((kind, channel_id), 0.0)
            return (now - last) >= DEAD_CHANNEL_COOLDOWN_S
        return True


def _record_chime(channel_id: Optional[int], kind: str) -> None:
    now = time.time()
    with _state_lock:
        _chime_timestamps.append(now)
        _prune_deque(_chime_timestamps, now - DAILY_WINDOW_S)
        if channel_id is not None:
            _channel_last_chime[channel_id] = now
        _kind_last[kind] = now
        if channel_id is not None:
            _kind_channel_last[(kind, channel_id)] = now
        _stats["last_chime_ts"] = now
        _stats["chimes_by_kind"][kind] = _stats["chimes_by_kind"].get(kind, 0) + 1
        # Recompute "chimes_today" as rolling 24h count
        _stats["chimes_today"] = len(_chime_timestamps)


# ---------------------------------------------------------------------------
# Classifier gate
# ---------------------------------------------------------------------------
_CLASSIFIER_SYSTEM = """you gate whether nexus — the ai member of a small discord called the nexus collective — should chime in unprompted.

return JSON ONLY, no prose:
{"chime": true|false, "reason": "<short>", "confidence": 0.0-1.0, "kind": "answer_question|memory_callback|conflict_deescalation|celebration|orient_new|other"}

the bar is HIGH. when in doubt: chime=false.

DO NOT chime on:
- small talk, vibes, banter, jokes (never be a buzzkill on someone's joke)
- one-line acks ("ok", "cool", "lol", "true", "fair")
- gaming chatter unless someone is directly asking a question
- venting where the person isn't asking for help
- anything already answered by another human
- messages under ~12 chars or pure emoji

DO chime on:
- a genuine question that's gone unanswered for 2+ minutes AND nexus plausibly has relevant memory/context (kind=answer_question)
- explicit reference to a past convo nexus might remember ("remember when we..." / "what did deadly say about...") (kind=memory_callback)
- visible distress or conflict where a calm grounded voice helps (kind=conflict_deescalation)
- a new member is visibly confused about the server or how things work (kind=orient_new)
- a major personal or group win worth naming (kind=celebration)

confidence scale:
- 0.9+  obvious, nexus would look silent-treatment if it skipped
- 0.7-0.89 solid case, lean in
- <0.7  skip. output chime=false regardless of kind.

output must be valid JSON with exactly those four keys. no explanation, no markdown."""


def _classifier_cache_get(text: str) -> Optional[dict]:
    now = time.time()
    with _state_lock:
        # Opportunistic prune
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


def _format_classifier_prompt(message_text: str, recent: list[dict]) -> str:
    ctx_lines = []
    for m in recent[-6:]:
        author = m.get("author", "?")
        content = (m.get("content") or "").replace("\n", " ")[:220]
        ctx_lines.append(f"{author}: {content}")
    ctx = "\n".join(ctx_lines) if ctx_lines else "(no prior context)"
    return (
        f"recent channel context (oldest first):\n{ctx}\n\n"
        f"new message to classify:\n{message_text[:600]}"
    )


async def _classify(message_text: str, recent: list[dict]) -> dict:
    """Call Haiku. Returns {chime, reason, confidence, kind}. Fails closed."""
    fallback = {"chime": False, "reason": "classifier_failed", "confidence": 0.0, "kind": "other"}
    text_norm = (message_text or "").strip()
    if not text_norm:
        return fallback

    cache_key = text_norm[:600]
    cached = _classifier_cache_get(cache_key)
    if cached is not None:
        _log(f"classifier cache hit: chime={cached.get('chime')} kind={cached.get('kind')}")
        return cached

    user_prompt = _format_classifier_prompt(text_norm, recent)
    _bump_classifier_counter()

    try:
        client = _get_client()
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=120,
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
            "chime": bool(data.get("chime", False)),
            "reason": str(data.get("reason", ""))[:200],
            "confidence": float(data.get("confidence", 0.0) or 0.0),
            "kind": str(data.get("kind", "other")).lower(),
        }
        valid_kinds = {
            "answer_question", "memory_callback", "conflict_deescalation",
            "celebration", "orient_new", "other",
        }
        if decision["kind"] not in valid_kinds:
            decision["kind"] = "other"
        _classifier_cache_put(cache_key, decision)
        _log(
            f"classifier: chime={decision['chime']} conf={decision['confidence']:.2f} "
            f"kind={decision['kind']} reason={decision['reason']!r}"
        )
        return decision
    except Exception as e:
        _log(f"classifier error ({type(e).__name__}): {e}")
        return fallback


# ---------------------------------------------------------------------------
# Reply path helpers
# ---------------------------------------------------------------------------
async def _gather_recent_context(
    channel: discord.abc.Messageable, skip_msg_id: Optional[int] = None
) -> list[dict]:
    """Return [{'author': str, 'content': str}, ...] oldest-first, excluding the trigger message."""
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
                "content": content[:400],
            })
            if len(lines) >= RECENT_CONTEXT_LIMIT:
                break
    except discord.Forbidden:
        return []
    except Exception as e:
        _log(f"history read error: {type(e).__name__}: {e}")
        return []
    lines.reverse()  # oldest-first, matching reply() convention
    return lines


async def _generate_and_send(
    channel: discord.abc.Messageable,
    user_name: str,
    user_message: str,
    user_id: Optional[str],
    recent_context: list[dict],
    kind: str,
) -> bool:
    """Run nexus_brain.reply() in a thread, send to channel (not as reply)."""
    try:
        reply_text = await asyncio.to_thread(
            nexus_brain.reply,
            user_name,
            user_message,
            recent_context,
            user_id,
        )
    except Exception as e:
        _log(f"brain.reply error ({type(e).__name__}): {e}")
        return False

    if not reply_text or not reply_text.strip():
        _log("brain.reply returned empty, skipping send")
        return False

    try:
        sent = await channel.send(reply_text)
    except discord.Forbidden:
        _log(f"forbidden sending to channel {getattr(channel, 'id', '?')}")
        return False
    except Exception as e:
        _log(f"send error ({type(e).__name__}): {e}")
        return False

    channel_id = getattr(channel, "id", None)
    _record_chime(channel_id, kind)
    # Open the continuation window, scoped to the user we chimed at (if any).
    # For proactive chimes with no specific recipient (user_id=None), the
    # window is still recorded but won't match any user's next message —
    # continuation is a no-op in that case by design.
    try:
        import nexus_continuation
        if channel_id is not None:
            try:
                _cont_uid = int(user_id) if user_id is not None else None
            except (TypeError, ValueError):
                _cont_uid = None
            nexus_continuation.mark_replied(channel_id, user_id=_cont_uid)
    except Exception:
        pass
    # Stamp for feedback learning (reaction emojis tell us if it landed)
    try:
        import nexus_feedback
        nexus_feedback.stamp_chime(
            sent, kind=f"proactive_{kind}", confidence=0.0, scope="tnc"
        )
    except Exception:
        pass
    _log(
        f"chimed kind={kind} ch={getattr(channel, 'name', channel_id)} "
        f"len={len(reply_text)} preview={reply_text[:80]!r}"
    )
    return True


# ---------------------------------------------------------------------------
# Public: try_chime_text
# ---------------------------------------------------------------------------
async def try_chime_text(message: discord.Message) -> bool:
    """Hook from on_message AFTER the direct 'nexus' trigger check.

    Returns True if Nexus posted. False otherwise (the common case).
    Never raises — all exceptions are caught and logged.
    """
    try:
        # Basic skip conditions — don't even run the classifier
        if message.author.bot:
            return False
        channel = message.channel
        if channel is None:
            return False

        content = (message.content or "").strip()
        if len(content) < MIN_MESSAGE_CHARS:
            return False

        # Consent gates first — cheapest way to bail
        if _consent_is_quiet():
            return False
        if _consent_is_shy(message.author.id):
            return False

        # Opt-out: if the user opted out of being recorded, also don't chime at them
        try:
            if nexus_consent is not None and hasattr(nexus_consent, "is_opted_out"):
                if nexus_consent.is_opted_out(message.author.id):
                    return False
        except Exception:
            pass

        channel_id = getattr(channel, "id", 0)

        # Cheap cooldown + budget checks BEFORE the classifier call so
        # a flood of messages in one channel doesn't burn Haiku tokens.
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            return False

        # Pull recent context (also used for reply path)
        recent = await _gather_recent_context(channel, skip_msg_id=message.id)

        # Classifier gate
        decision = await _classify(content, recent)
        if not decision.get("chime"):
            return False
        if float(decision.get("confidence", 0.0)) < CONFIDENCE_THRESHOLD:
            _log(
                f"confidence {decision.get('confidence'):.2f} below threshold "
                f"{CONFIDENCE_THRESHOLD}, skipping"
            )
            return False

        # Re-check cooldown + budget (another coroutine might have chimed during classify)
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            _log("budget depleted mid-flight, skipping")
            return False

        kind = decision.get("kind", "other")

        # Skill graph injection — if this looks like a question/help-seek and
        # someone in TNC has declared a matching skill, surface them so nexus
        # can connect rather than answer. Self-gated inside skills_for_query
        # (returns [] when not applicable).
        try:
            import nexus_skills
            matches = await nexus_skills.skills_for_query(
                content,
                exclude_user_id=str(message.author.id),
                limit=3,
            )
            if matches:
                hint = nexus_skills.format_for_classifier(matches)
                if hint:
                    recent = list(recent) + [{
                        "author": "[nexus internal]",
                        "content": hint,
                    }]
                    _log(f"skill matches injected ({len(matches)}) for kind={kind}")
        except Exception as _e:
            _log(f"skill match inject failed: {type(_e).__name__}: {_e}")

        return await _generate_and_send(
            channel=channel,
            user_name=message.author.display_name,
            user_message=content,
            user_id=str(message.author.id),
            recent_context=recent,
            kind=kind,
        )
    except Exception as e:
        _log(f"try_chime_text fatal ({type(e).__name__}): {e}")
        return False


# ---------------------------------------------------------------------------
# Public: try_chime_voice
# ---------------------------------------------------------------------------
async def try_chime_voice(voice_channel, recent_lines: list[dict]) -> bool:
    """Called by voice-opening detector.

    recent_lines: last ~10 voice utterances [{'name': str, 'text': str, 'ts': float}, ...]
    Decide if Nexus has something worth saying, generate, speak into VC.
    """
    try:
        if not recent_lines:
            return False

        # Consent — server-wide quiet also mutes voice chimes
        if _consent_is_quiet():
            return False

        # Find the voice client
        vc = None
        try:
            guild = getattr(voice_channel, "guild", None)
            if guild is not None:
                vc = guild.voice_client
        except Exception:
            vc = None
        if vc is None or not getattr(vc, "is_connected", lambda: False)():
            _log("voice chime skipped — no connected voice_client")
            return False

        channel_id = getattr(voice_channel, "id", 0)
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            return False

        # Build a classifier prompt from voice lines
        recent_for_classifier = []
        for ln in recent_lines[-8:]:
            recent_for_classifier.append({
                "author": ln.get("name", "someone"),
                "content": (ln.get("text") or "").strip()[:220],
            })

        last = recent_lines[-1]
        last_text = (last.get("text") or "").strip()
        if len(last_text) < MIN_MESSAGE_CHARS:
            return False

        # Respect per-speaker shy flag if the speaker's id is threaded through
        last_user_id = last.get("user_id") or last.get("id")
        if last_user_id is not None:
            try:
                if _consent_is_shy(int(last_user_id)):
                    return False
            except (TypeError, ValueError):
                pass

        decision = await _classify(last_text, recent_for_classifier)
        if not decision.get("chime"):
            return False
        if float(decision.get("confidence", 0.0)) < CONFIDENCE_THRESHOLD:
            return False

        # Recheck gates after async work
        if not _channel_cooldown_ok(channel_id):
            return False
        if not _budget_available():
            return False

        # Generate reply via nexus_brain.reply() — same pipeline as text
        recent_ctx_for_reply = [
            {"author": ln.get("name", "someone"), "content": (ln.get("text") or "")[:400]}
            for ln in recent_lines[-RECENT_CONTEXT_LIMIT:]
        ]
        try:
            reply_text = await asyncio.to_thread(
                nexus_brain.reply,
                last.get("name", "someone"),
                last_text,
                recent_ctx_for_reply,
                str(last_user_id) if last_user_id is not None else None,
            )
        except Exception as e:
            _log(f"voice brain.reply error ({type(e).__name__}): {e}")
            return False

        if not reply_text or not reply_text.strip():
            return False

        # Speak it. nexus_voice exposes synthesize() → Path. No module-level
        # speak() exists yet, so we do the standard synthesize + FFmpegPCMAudio
        # dance inline (mirrors nexus_bot._handle_voice_trigger).
        if nexus_voice is None:
            _log("nexus_voice not importable, cannot speak")
            return False

        try:
            path = await nexus_voice.synthesize(reply_text)
        except Exception as e:
            _log(f"tts synthesize error ({type(e).__name__}): {e}")
            return False

        try:
            if vc.is_playing():
                vc.stop()
            source = discord.FFmpegPCMAudio(str(path))
            vc.play(source, after=nexus_voice.cleanup_callback(path))
        except FileNotFoundError as e:
            _log(f"ffmpeg missing: {e}")
            return False
        except Exception as e:
            _log(f"vc.play error ({type(e).__name__}): {e}")
            return False

        kind = decision.get("kind", "other")
        _record_chime(channel_id, f"voice_{kind}")
        _log(f"voice chime kind=voice_{kind} preview={reply_text[:80]!r}")
        return True
    except Exception as e:
        _log(f"try_chime_voice fatal ({type(e).__name__}): {e}")
        return False


# ---------------------------------------------------------------------------
# Public: try_chime_admin
# ---------------------------------------------------------------------------
_ADMIN_KINDS = {"dead_channel", "unanswered_question", "weekly_digest", "new_member_orient", "followup"}


async def try_chime_admin(channel, kind: str, payload: dict) -> bool:
    """Called by caretaker. kind ∈ _ADMIN_KINDS."""
    try:
        if kind not in _ADMIN_KINDS:
            _log(f"admin chime unknown kind={kind!r}, ignoring")
            return False
        if channel is None:
            return False

        if _consent_is_quiet():
            _log(f"admin chime {kind} skipped — quiet mode")
            return False

        channel_id = getattr(channel, "id", 0)

        # Kind-specific cooldowns
        if not _kind_cooldown_ok(kind, channel_id):
            _log(f"admin chime {kind} skipped — kind cooldown")
            return False
        # Channel cooldown applies too (except weekly_digest which is server-wide-ish)
        if kind != "weekly_digest" and not _channel_cooldown_ok(channel_id):
            _log(f"admin chime {kind} skipped — channel cooldown")
            return False
        if not _budget_available():
            _log(f"admin chime {kind} skipped — daily cap")
            return False

        # Build a kind-specific system augment + user message for nexus_brain.reply().
        # We use reply() so the voice stays consistent with everything else.
        payload = payload or {}
        user_name, user_msg = _build_admin_prompt(kind, payload, channel)
        recent = await _gather_recent_context(channel)

        return await _generate_and_send(
            channel=channel,
            user_name=user_name,
            user_message=user_msg,
            user_id=None,
            recent_context=recent,
            kind=kind,
        )
    except Exception as e:
        _log(f"try_chime_admin fatal ({type(e).__name__}): {e}")
        return False


def _build_admin_prompt(kind: str, payload: dict, channel) -> tuple[str, str]:
    """Return (user_name, user_message) used to drive nexus_brain.reply().

    The message is framed as an internal nudge from 'caretaker' so nexus replies
    in-character rather than echoing the nudge. Lowercase, terse.
    """
    ch_name = getattr(channel, "name", "this channel")
    if kind == "dead_channel":
        days = payload.get("days_dead", "a few")
        last_author = payload.get("last_msg_author", "someone")
        msg = (
            f"[caretaker nudge] #{ch_name} has been quiet for {days} days. "
            f"last in was {last_author}. drop one short grounded line that could "
            f"re-open things — no questions, no pings, no forced energy. "
            f"one sentence. lowercase. skip if nothing real comes to mind."
        )
        return ("caretaker", msg)
    if kind == "unanswered_question":
        author = payload.get("author", "someone")
        question = payload.get("question", "")
        age_min = payload.get("age_minutes", "a while")
        msg = (
            f"[caretaker nudge] {author} asked this in #{ch_name} {age_min}m ago "
            f"and no one answered:\n"
            f"  > {question}\n"
            f"if you actually have something useful (from memory or reasoning), "
            f"answer it in one or two lines. if you don't, say so briefly. "
            f"address them by name."
        )
        return (author, msg)
    if kind == "weekly_digest":
        highlights = payload.get("highlights") or []
        bullet = "\n".join(f"- {h}" for h in highlights[:12]) or "- (no highlights provided)"
        msg = (
            f"[caretaker nudge] weekly digest time. here's what actually happened "
            f"across tnc in the last ~7 days:\n{bullet}\n\n"
            f"write 3-6 short lines summarizing the week in your voice. no bullet "
            f"list, no headings, just prose. name people where it matters. keep it "
            f"real — if the week was thin, say that."
        )
        return ("caretaker", msg)
    if kind == "new_member_orient":
        name = payload.get("member_name", "the new person")
        msg = (
            f"[caretaker nudge] {name} just joined and looks confused. "
            f"drop a single warm short line in #{ch_name} that orients them — "
            f"what this server is, where to go next. no wall of text. lowercase. "
            f"use their name."
        )
        return (name, msg)
    if kind == "followup":
        target_name = payload.get("user_name", "someone")
        target_uid = payload.get("user_id", "")
        hook = payload.get("hook", "something they mentioned")
        nudge = (payload.get("nudge_text") or "").strip()
        if nudge:
            msg = (
                f"[caretaker nudge] post this in #{ch_name} verbatim — do not "
                f"rewrite, do not add anything: {nudge}"
            )
        else:
            mention = f"<@{target_uid}>" if target_uid else target_name
            msg = (
                f"[caretaker nudge] {target_name} mentioned earlier: \"{hook}\". "
                f"check in on them in #{ch_name} with {mention}. one short warm line, "
                f"lowercase. e.g. 'how'd <hook> go?' or 'did you end up doing <hook>?'. "
                f"no stacked questions. skip if it'd feel forced."
            )
        return (target_name, msg)
    # Fallback — shouldn't hit because we gate on _ADMIN_KINDS
    return ("caretaker", f"[caretaker nudge] ({kind}) {json.dumps(payload)[:400]}")


# ---------------------------------------------------------------------------
# Public: install + stats
# ---------------------------------------------------------------------------
def install(bot, guild_id: int) -> None:
    """Cache bot ref + guild. Idempotent — safe to call multiple times."""
    global _bot, _guild_id
    _bot = bot
    _guild_id = int(guild_id)
    _log(
        f"installed — channel_cooldown={CHANNEL_COOLDOWN_S}s daily_cap={DAILY_CAP} "
        f"confidence>={CONFIDENCE_THRESHOLD} model={CLASSIFIER_MODEL}"
    )


def get_stats() -> dict:
    """Snapshot of current proactivity state for /diag."""
    now = time.time()
    with _state_lock:
        _prune_deque(_chime_timestamps, now - DAILY_WINDOW_S)
        _prune_deque(_stats["classifier_calls_ts"], now - DAILY_WINDOW_S)
        budget_remaining = max(0, DAILY_CAP - len(_chime_timestamps))
        # Active cooldowns = channels whose last chime is within CHANNEL_COOLDOWN_S
        cooldowns_active: dict[int, float] = {}
        for ch_id, last in _channel_last_chime.items():
            remaining = CHANNEL_COOLDOWN_S - (now - last)
            if remaining > 0:
                cooldowns_active[ch_id] = round(remaining, 1)
        return {
            "classifier_calls_today": len(_stats["classifier_calls_ts"]),
            "chimes_today": len(_chime_timestamps),
            "chimes_by_kind": dict(_stats["chimes_by_kind"]),
            "budget_remaining": budget_remaining,
            "cooldowns_active": cooldowns_active,
            "last_chime_ts": _stats["last_chime_ts"],
            "tunables": {
                "channel_cooldown_s": CHANNEL_COOLDOWN_S,
                "daily_cap": DAILY_CAP,
                "confidence_threshold": CONFIDENCE_THRESHOLD,
                "classifier_model": CLASSIFIER_MODEL,
            },
        }


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------
__all__ = [
    "install",
    "try_chime_text",
    "try_chime_voice",
    "try_chime_admin",
    "get_stats",
    # tunables for observability
    "CHANNEL_COOLDOWN_S",
    "DAILY_CAP",
    "CONFIDENCE_THRESHOLD",
    "CLASSIFIER_MODEL",
]
