"""
Nexus caretaker — background admin loop.

Every ~30 min Nexus quietly tends the server:
  1. Revives dead channels (>5 days silent) — once per channel per 7 days.
  2. Flags unanswered questions (last 2h, no human reply in 60min).
  3. Posts a weekly digest on Sunday 21:00-22:00 local time.

All action is emitted via `nexus_proactive.try_chime_admin(channel, kind, payload)`.
If that module isn't loaded yet, falls back to a stub log line (no posts made).

Install:
    import nexus_caretaker
    nexus_caretaker.install(bot, DISCORD_GUILD_ID)   # call in on_ready
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Optional

import discord

import config

# ---------------------------------------------------------------------------
# Tunables — env-overridable
# ---------------------------------------------------------------------------
# Warmup after bot start (let everything settle before first pass)
CARETAKER_WARMUP_S = int(os.environ.get("CARETAKER_WARMUP_S", 10 * 60))  # 10 min

# Time between full cycles
CARETAKER_INTERVAL_S = int(os.environ.get("CARETAKER_INTERVAL_S", 30 * 60))  # 30 min
# Small random jitter so multiple restarts don't align
CARETAKER_INTERVAL_JITTER_S = int(os.environ.get("CARETAKER_INTERVAL_JITTER_S", 5 * 60))

# Check 1 — dead channel
DEAD_CHANNEL_DAYS = int(os.environ.get("CARETAKER_DEAD_DAYS", 5))
DEAD_CHANNEL_COOLDOWN_DAYS = int(os.environ.get("CARETAKER_DEAD_COOLDOWN_DAYS", 7))

# Check 2 — unanswered questions
QUESTION_LOOKBACK_H = int(os.environ.get("CARETAKER_QUESTION_LOOKBACK_H", 2))
QUESTION_GRACE_MIN = int(os.environ.get("CARETAKER_QUESTION_GRACE_MIN", 60))
QUESTION_MIN_LEN = 15
FLAGGED_QUESTIONS_CAP = 200
# Per-channel history fetch cap for question scan
QUESTION_PER_CHANNEL_LIMIT = 80

# Check 3 — weekly digest
DIGEST_WEEKDAY = 6          # Monday=0 ... Sunday=6
DIGEST_HOUR_START = 21
DIGEST_HOUR_END = 22        # exclusive upper bound (fires 21:00–21:59)
DIGEST_LOOKBACK_DAYS = 7

# State file — lives next to the bot
STATE_PATH = Path(config.ROOT) / "caretaker_state.json"
_STATE_LOCK = threading.Lock()

THOUGHTS_CHANNEL = getattr(config, "CHANNEL_THOUGHTS", "thoughts")
VOICE_TRANSCRIPTS_PATH = Path(config.ROOT) / "voice_transcripts.jsonl"


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_caretaker] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _default_state() -> dict:
    return {
        "last_revival": {},          # {channel_id_str: iso_ts}
        "flagged_questions": [],     # [msg_id_str, ...] capped
        "last_digest_week": None,    # "2026-W17"
        "first_observed": {},        # {channel_id_str: iso_ts} — when caretaker first saw channel
    }


def _load_state() -> dict:
    with _STATE_LOCK:
        if not STATE_PATH.exists():
            return _default_state()
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            # Back-fill any missing keys if someone hand-edited it
            default = _default_state()
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            _log(f"state load error: {type(e).__name__}: {e} — starting fresh")
            return _default_state()


def _save_state(state: dict) -> None:
    with _STATE_LOCK:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            _log(f"state save error: {type(e).__name__}: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Admin chime emit — graceful stub when nexus_proactive not available
# ---------------------------------------------------------------------------
async def _emit_admin(channel: discord.TextChannel, kind: str, payload: dict) -> bool:
    """Try to fire a proactive admin chime. Falls back to a stub log line."""
    try:
        import nexus_proactive  # type: ignore
        fn = getattr(nexus_proactive, "try_chime_admin", None)
        if fn is None:
            raise AttributeError("try_chime_admin")
        result = await fn(channel, kind, payload)
        return bool(result)
    except (ImportError, AttributeError):
        _log(
            f"[stub] would emit admin chime: kind={kind} "
            f"channel=#{getattr(channel, 'name', '?')} payload={payload}"
        )
        return False
    except Exception as e:
        _log(f"emit_admin error ({kind}): {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Channel targeting helpers
# ---------------------------------------------------------------------------
def _listen_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """Return text channels in the configured listen list."""
    out: list[discord.TextChannel] = []
    for ch in guild.text_channels:
        canon = config.canon_channel(ch.name)
        if canon in config.NEXUS_IGNORE_CHANNELS:
            continue
        if canon not in config.NEXUS_LISTEN_CHANNELS:
            continue
        out.append(ch)
    return out


def _find_thoughts_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    target = THOUGHTS_CHANNEL.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == target:
            return ch
        if config.canon_channel(ch.name) == target:
            return ch
    return None


# ---------------------------------------------------------------------------
# Check 1 — dead channel revival
# ---------------------------------------------------------------------------
async def _check_dead_channels(guild: discord.Guild, state: dict) -> int:
    """For each listen channel: if last msg older than DEAD_CHANNEL_DAYS, chime.

    Honors per-channel cooldown DEAD_CHANNEL_COOLDOWN_DAYS. Returns # chimed.
    """
    now = dt.datetime.now(dt.timezone.utc)
    dead_threshold = dt.timedelta(days=DEAD_CHANNEL_DAYS)
    cooldown = dt.timedelta(days=DEAD_CHANNEL_COOLDOWN_DAYS)

    chimed = 0
    checked = 0
    first_observed = state.setdefault("first_observed", {})
    for ch in _listen_channels(guild):
        checked += 1
        try:
            ch_key = str(ch.id)
            # Cold-start guard: stamp first time we see this channel, then skip
            # chiming until we've been observing it for DEAD_CHANNEL_DAYS.
            # Otherwise a fresh install fires a flood on its very first cycle.
            if ch_key not in first_observed:
                first_observed[ch_key] = now.isoformat()
                _log(f"first_observed seeded for #{ch.name} — skipping dead check this cycle")
                continue
            try:
                fo = dt.datetime.fromisoformat(first_observed[ch_key])
                if fo.tzinfo is None:
                    fo = fo.replace(tzinfo=dt.timezone.utc)
                if now - fo < dead_threshold:
                    continue
            except Exception:
                # Malformed — refresh and skip this cycle
                first_observed[ch_key] = now.isoformat()
                continue

            last_msg: Optional[discord.Message] = None
            async for msg in ch.history(limit=1):
                last_msg = msg
                break
            if last_msg is None:
                # Empty channel — treat as "dead" but only once per cooldown
                last_ts = None
                days_dead = None
                last_author = "(nobody)"
            else:
                last_ts = last_msg.created_at
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=dt.timezone.utc)
                days_dead_td = now - last_ts
                if days_dead_td < dead_threshold:
                    continue
                days_dead = days_dead_td.days
                last_author = (
                    last_msg.author.display_name
                    if last_msg.author else "unknown"
                )

            # Cooldown check
            ch_key = str(ch.id)
            prior_iso = state["last_revival"].get(ch_key)
            if prior_iso:
                try:
                    prior = dt.datetime.fromisoformat(prior_iso)
                    if prior.tzinfo is None:
                        prior = prior.replace(tzinfo=dt.timezone.utc)
                    if now - prior < cooldown:
                        continue
                except Exception:
                    pass  # malformed — fall through and refresh

            payload = {
                "days_dead": days_dead if days_dead is not None else -1,
                "last_author": last_author,
            }
            _log(f"dead channel detected: #{ch.name} ({payload['days_dead']}d since {last_author})")
            fired = await _emit_admin(ch, "dead_channel", payload)
            if not fired:
                # stub path — still mark so we don't spam logs every cycle
                _log(f"would revive dead channel #{ch.name} ({payload['days_dead']}d)")
            state["last_revival"][ch_key] = now.isoformat()
            chimed += 1
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"dead check error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    _log(f"check1 dead_channels: scanned={checked} chimed={chimed}")
    return chimed


# ---------------------------------------------------------------------------
# Check 2 — unanswered questions
# ---------------------------------------------------------------------------
def _looks_like_question(msg: discord.Message) -> bool:
    content = (msg.content or "").strip()
    if not content.endswith("?"):
        return False
    if len(content) < QUESTION_MIN_LEN:
        return False
    if msg.author.bot:
        return False
    if content.startswith("/") or content.startswith("!"):
        return False
    return True


async def _check_unanswered_questions(guild: discord.Guild, state: dict) -> int:
    """Scan listen channels; flag questions with no human reply in grace window."""
    now = dt.datetime.now(dt.timezone.utc)
    lookback = dt.timedelta(hours=QUESTION_LOOKBACK_H)
    since = now - lookback
    grace = dt.timedelta(minutes=QUESTION_GRACE_MIN)

    flagged_set = set(state.get("flagged_questions", []))
    chimed = 0
    scanned = 0

    for ch in _listen_channels(guild):
        try:
            # Collect recent messages, newest last
            msgs: list[discord.Message] = []
            async for m in ch.history(limit=QUESTION_PER_CHANNEL_LIMIT, after=since, oldest_first=True):
                msgs.append(m)
            scanned += len(msgs)

            for i, q in enumerate(msgs):
                if not _looks_like_question(q):
                    continue

                q_ts = q.created_at
                if q_ts.tzinfo is None:
                    q_ts = q_ts.replace(tzinfo=dt.timezone.utc)
                age = now - q_ts
                # Must have waited at least grace period before flagging
                if age < grace:
                    continue

                mid = str(q.id)
                if mid in flagged_set:
                    continue

                # Check reactions (any reaction = engagement)
                has_reaction = False
                try:
                    for r in q.reactions:
                        if (r.count or 0) > 0:
                            has_reaction = True
                            break
                except Exception:
                    pass
                if has_reaction:
                    continue

                # Check for any human reply in same channel within grace of the question
                answered = False
                for j in range(i + 1, len(msgs)):
                    r = msgs[j]
                    r_ts = r.created_at
                    if r_ts.tzinfo is None:
                        r_ts = r_ts.replace(tzinfo=dt.timezone.utc)
                    if r_ts - q_ts > grace:
                        break
                    if r.author.bot:
                        continue
                    if r.author.id == q.author.id:
                        continue  # same asker talking to themselves
                    answered = True
                    break
                if answered:
                    continue

                payload = {
                    "question": (q.content or "").strip()[:400],
                    "asker": q.author.display_name,
                    "msg_id": q.id,
                    "minutes_old": int(age.total_seconds() // 60),
                }
                _log(
                    f"unanswered question flagged in #{ch.name} "
                    f"by {payload['asker']} ({payload['minutes_old']}m old)"
                )
                await _emit_admin(ch, "unanswered_question", payload)
                flagged_set.add(mid)
                chimed += 1

        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"question check error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    # Cap flagged list to last N (keep newest = largest msg ids)
    if len(flagged_set) > FLAGGED_QUESTIONS_CAP:
        # ints sort naturally; fall back to str sort if not numeric
        try:
            sorted_ids = sorted(flagged_set, key=lambda x: int(x))
        except Exception:
            sorted_ids = sorted(flagged_set)
        flagged_set = set(sorted_ids[-FLAGGED_QUESTIONS_CAP:])

    state["flagged_questions"] = list(flagged_set)
    _log(f"check2 unanswered_questions: scanned_msgs={scanned} flagged={chimed}")
    return chimed


# ---------------------------------------------------------------------------
# Check 3 — weekly digest
# ---------------------------------------------------------------------------
def _iso_week_key(now_local: dt.datetime) -> str:
    iso = now_local.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _count_voice_calls(since_utc: dt.datetime) -> int:
    """Heuristic: count 'call starts' by grouping contiguous voice utterances.

    A call = gap >= 15 min between utterances starts a new call.
    """
    if not VOICE_TRANSCRIPTS_PATH.exists():
        return 0
    call_gap = 15 * 60
    count = 0
    last_ts: Optional[float] = None
    try:
        with open(VOICE_TRANSCRIPTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                rec_dt = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
                if rec_dt < since_utc:
                    last_ts = ts  # still update for gap math
                    continue
                if last_ts is None or (ts - last_ts) > call_gap:
                    count += 1
                last_ts = ts
    except Exception as e:
        _log(f"voice transcript read error: {type(e).__name__}: {e}")
    return count


async def _build_digest(guild: discord.Guild) -> tuple[str, dict]:
    """Build a clean lowercase weekly digest. Returns (summary, stats)."""
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=DIGEST_LOOKBACK_DAYS)

    msg_counts: Counter[str] = Counter()     # channel canon -> count
    author_counts: Counter[str] = Counter()  # display_name -> count
    watched_videos = 0
    new_members: list[str] = []

    bot_id = guild.me.id if guild.me else 0

    for ch in _listen_channels(guild):
        try:
            async for m in ch.history(limit=500, after=since):
                if m.author.bot:
                    # Count /watch embeds from nexus as watched videos (rough)
                    if m.author.id == bot_id and m.embeds:
                        for emb in m.embeds:
                            url = getattr(emb, "url", None) or ""
                            if "youtu" in url:
                                watched_videos += 1
                                break
                    continue
                canon = config.canon_channel(ch.name)
                msg_counts[canon] += 1
                author_counts[m.author.display_name] += 1
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"digest history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    # New members (best-effort — guild.members needs members intent)
    try:
        for member in guild.members:
            joined = getattr(member, "joined_at", None)
            if joined is None:
                continue
            if joined.tzinfo is None:
                joined = joined.replace(tzinfo=dt.timezone.utc)
            if joined >= since and not member.bot:
                new_members.append(member.display_name)
    except Exception:
        pass

    voice_calls = _count_voice_calls(since)

    top_channels = msg_counts.most_common(5)
    top_humans = author_counts.most_common(3)
    total_msgs = sum(msg_counts.values())

    # Build lowercase summary ~6-10 lines
    lines: list[str] = []
    lines.append("weekly digest — past 7 days")
    lines.append(f"{total_msgs} messages across {len(msg_counts)} channels")
    if top_channels:
        tc = ", ".join(f"#{name} ({n})" for name, n in top_channels[:3])
        lines.append(f"busiest: {tc}")
    if top_humans:
        th = ", ".join(f"{name} ({n})" for name, n in top_humans)
        lines.append(f"top voices: {th}")
    lines.append(f"voice calls: {voice_calls}")
    if watched_videos:
        lines.append(f"videos watched: {watched_videos}")
    if new_members:
        lines.append(f"new members: {', '.join(new_members[:5])}")
    else:
        lines.append("new members: none")
    lines.append("quiet weeks are fine. just a pulse check.")

    summary = "\n".join(lines)
    stats = {
        "total_msgs": total_msgs,
        "top_channels": top_channels,
        "top_humans": top_humans,
        "voice_calls": voice_calls,
        "watched_videos": watched_videos,
        "new_members": new_members,
        "lookback_days": DIGEST_LOOKBACK_DAYS,
    }
    return summary, stats


async def _check_weekly_digest(guild: discord.Guild, state: dict) -> bool:
    """Fire once per ISO week on Sunday 21:00-22:00 local. Returns True if fired."""
    now_local = dt.datetime.now()   # naive local time — intended, that's our window
    if now_local.weekday() != DIGEST_WEEKDAY:
        return False
    if not (DIGEST_HOUR_START <= now_local.hour < DIGEST_HOUR_END):
        return False
    week_key = _iso_week_key(now_local)
    if state.get("last_digest_week") == week_key:
        return False

    ch = _find_thoughts_channel(guild)
    if not ch:
        _log(f"digest: no #{THOUGHTS_CHANNEL} channel, skipping")
        return False

    summary, stats = await _build_digest(guild)
    payload = {"summary": summary, "stats": stats}
    _log(f"weekly digest ready ({stats['total_msgs']} msgs, week={week_key})")
    await _emit_admin(ch, "weekly_digest", payload)
    state["last_digest_week"] = week_key
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def _cycle(bot: discord.Client, guild_id: int) -> None:
    guild = bot.get_guild(guild_id)
    if not guild:
        _log(f"no guild {guild_id}, skipping cycle")
        return

    state = _load_state()

    # Each check isolated — one failure can't kill the others
    try:
        await _check_dead_channels(guild, state)
    except Exception as e:
        _log(f"check1 top-level error: {type(e).__name__}: {e}")

    try:
        await _check_unanswered_questions(guild, state)
    except Exception as e:
        _log(f"check2 top-level error: {type(e).__name__}: {e}")

    try:
        await _check_weekly_digest(guild, state)
    except Exception as e:
        _log(f"check3 top-level error: {type(e).__name__}: {e}")

    # Person-level follow-ups: nudge people about things they mentioned earlier
    # (tests, trips, presentations). Self-gated on per-user/server cooldowns.
    try:
        import nexus_followups
        n_fired = await nexus_followups.dispatch_due(bot)
        if n_fired:
            _log(f"followups dispatched: {n_fired}")
    except Exception as e:
        _log(f"check4 (followups) top-level error: {type(e).__name__}: {e}")

    # Daily morning digest in #dev-logs. Self-gated on time window + state.
    try:
        import nexus_digest
        await nexus_digest.maybe_post_daily(bot)
    except Exception as e:
        _log(f"check5 (digest) top-level error: {type(e).__name__}: {e}")

    _save_state(state)


async def _loop(bot: discord.Client, guild_id: int) -> None:
    _log(
        f"caretaker loop started — warmup {CARETAKER_WARMUP_S}s, "
        f"cadence {CARETAKER_INTERVAL_S}s (+/- {CARETAKER_INTERVAL_JITTER_S}s)"
    )
    try:
        await asyncio.sleep(CARETAKER_WARMUP_S)
    except asyncio.CancelledError:
        raise

    while True:
        try:
            await _cycle(bot, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"cycle error: {type(e).__name__}: {e}")

        jitter = random.randint(-CARETAKER_INTERVAL_JITTER_S, CARETAKER_INTERVAL_JITTER_S)
        wait = max(60, CARETAKER_INTERVAL_S + jitter)
        _log(f"next cycle in {wait//60}m")
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_task: Optional[asyncio.Task] = None


def install(bot: discord.Client, guild_id: int) -> None:
    """Start the background caretaker loop. Safe to call multiple times; no-op after first."""
    global _task
    if _task and not _task.done():
        _log("already running")
        return
    _task = asyncio.create_task(_loop(bot, guild_id))
    _log("installed")


# ---------------------------------------------------------------------------
# Debug / force-fire helpers (for manual slash commands, optional)
# ---------------------------------------------------------------------------
async def force_cycle_now(bot: discord.Client, guild_id: int) -> dict:
    """Force one full cycle immediately. Returns a small summary dict."""
    guild = bot.get_guild(guild_id)
    if not guild:
        return {"ok": False, "reason": "no_guild"}
    state = _load_state()
    result = {"ok": True}
    try:
        result["dead"] = await _check_dead_channels(guild, state)
    except Exception as e:
        result["dead_err"] = f"{type(e).__name__}: {e}"
    try:
        result["questions"] = await _check_unanswered_questions(guild, state)
    except Exception as e:
        result["questions_err"] = f"{type(e).__name__}: {e}"
    try:
        result["digest_fired"] = await _check_weekly_digest(guild, state)
    except Exception as e:
        result["digest_err"] = f"{type(e).__name__}: {e}"
    _save_state(state)
    return result
