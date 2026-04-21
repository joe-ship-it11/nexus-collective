"""
Nexus mirror — the personal identity pillar.

Three features:

  /mirror  — ephemeral slash command. Nexus reads the caller's last 60-150
             messages across listen channels + last 14 days of voice
             transcripts, returns a one-paragraph "what era are you in
             right now" reading. Warm but sharp. Pattern recognition, not
             flattery. Caller-only; never exposes another user's data.

  /vibe    — ephemeral slash command. Nexus reads the caller's last 40
             messages and returns an energy read ("you've been running
             hot", "you're coasting", "anxious-masking"). 1-3 sentences.

  Weekly   — eigenquote scheduler. Every Sunday at 5pm local, Nexus picks
  eigenquote the most-"them" line of the week for each active member
             (10+ messages that week) and posts a single brand-blue embed
             in #💭│thoughts titled "quotes of the week".

Install:
    import nexus_mirror
    nexus_mirror.install(bot, DISCORD_GUILD_ID)   # call in on_ready

The installer registers both slash commands on the guild and starts the
eigenquote scheduler task. Idempotent — safe to call multiple times.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import time
from typing import Optional

import discord
from discord import app_commands

import config
import nexus_brain


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIRROR_MODEL = "claude-haiku-4-5-20251001"
MIRROR_TEMPERATURE = 0.85
MIRROR_MAX_TOKENS = 400

# /mirror — message windows
MIRROR_TEXT_MIN = 60
MIRROR_TEXT_MAX = 150
MIRROR_TEXT_LOOKBACK_DAYS = 30
MIRROR_PER_CHANNEL_LIMIT = 200
MIRROR_VOICE_LOOKBACK_DAYS = 14

# /vibe — tight window
VIBE_TEXT_LIMIT = 40
VIBE_TEXT_LOOKBACK_DAYS = 14

# Voice transcript filter (mirrors nexus_mind)
VOICE_MIN_CHARS = 18
VOICE_STOP_PHRASES = {
    "thank you.", "thanks.", "okay.", "ok.", "bye.", "mmhm.",
    "uh huh.", "yeah.", "mhm.", "alright.", "cool.", "yep.",
}

# Eigenquote scheduler
EIGENQUOTE_HOUR = 17          # 5pm local
EIGENQUOTE_WEEKDAY = 6        # Sunday (Monday=0, Sunday=6)
EIGENQUOTE_CHECK_INTERVAL = 60  # seconds
EIGENQUOTE_MIN_MESSAGES = 10  # min msgs/week to qualify
EIGENQUOTE_MAX_CANDIDATES_PER_USER = 25  # cap sent to Claude per person

THOUGHTS_CHANNEL = getattr(config, "CHANNEL_THOUGHTS", "thoughts")

EMBED_COLOR = 0x3B82F6  # brand blue

# State file — tracks last eigenquote fire date so we don't double-post
STATE_PATH = pathlib.Path(__file__).parent / "mirror_state.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_mirror] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"state read error: {type(e).__name__}: {e}")
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(state, indent=2, default=str), encoding="utf-8"
        )
    except Exception as e:
        _log(f"state write error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Channel / message helpers
# ---------------------------------------------------------------------------
def _find_thoughts_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    target = THOUGHTS_CHANNEL.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == target:
            return ch
        try:
            if config.canon_channel(ch.name) == target:
                return ch
        except Exception:
            continue
    return None


def _listen_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """Text channels that Nexus actively listens in (post-canon filter)."""
    out: list[discord.TextChannel] = []
    for ch in guild.text_channels:
        try:
            canon = config.canon_channel(ch.name)
        except Exception:
            canon = ch.name.lower()
        if canon in config.NEXUS_IGNORE_CHANNELS:
            continue
        if canon not in config.NEXUS_LISTEN_CHANNELS:
            continue
        out.append(ch)
    return out


def _is_substantive(content: str) -> bool:
    if not content:
        return False
    c = content.strip()
    if len(c) < 8:
        return False
    if c.startswith("/") or c.startswith("!"):
        return False
    return True


async def _fetch_user_messages(
    guild: discord.Guild,
    user_id: int,
    *,
    lookback_days: int,
    per_channel_limit: int,
    cap: int,
) -> list[dict]:
    """Pull this user's recent messages across listen channels.

    Returns [{channel, content, ts}] — newest last. Caller is the only
    subject; never include anyone else's text.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    out: list[dict] = []
    for ch in _listen_channels(guild):
        try:
            async for msg in ch.history(
                limit=per_channel_limit, after=since, oldest_first=False
            ):
                if msg.author.id != user_id:
                    continue
                if msg.author.bot:
                    continue
                content = (msg.content or "").strip()
                if not _is_substantive(content):
                    continue
                try:
                    canon = config.canon_channel(ch.name)
                except Exception:
                    canon = ch.name
                out.append({
                    "channel": canon,
                    "content": content[:400],
                    "ts": msg.created_at.isoformat(timespec="minutes"),
                })
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    # Sort newest first, trim, then flip chronological
    out.sort(key=lambda m: m.get("ts") or "", reverse=True)
    if len(out) > cap:
        out = out[:cap]
    out.reverse()
    return out


def _load_voice_lines_for_user(
    user_id: int,
    user_name: str,
    *,
    lookback_days: int,
    cap: int,
) -> list[dict]:
    """Pull the user's voice transcript lines from voice_transcripts.jsonl.

    Matching is by user_id (preferred) with name fallback. Filters the
    standard Whisper-hallucination stop-phrases and short lines.
    """
    path = pathlib.Path(__file__).parent / "voice_transcripts.jsonl"
    if not path.exists():
        return []

    cutoff = time.time() - (lookback_days * 86400)
    out: list[dict] = []
    uid_str = str(user_id)
    name_low = (user_name or "").lower()
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
                # user match: prefer id, fall back to name (case-insensitive)
                rec_uid = str(rec.get("user_id") or "")
                rec_name = (rec.get("name") or "").lower()
                if rec_uid:
                    if rec_uid != uid_str:
                        continue
                elif name_low and rec_name != name_low:
                    continue
                text = (rec.get("text") or "").strip()
                if len(text) < VOICE_MIN_CHARS:
                    continue
                if text.lower() in VOICE_STOP_PHRASES:
                    continue
                iso = rec.get("iso") or ""
                out.append({
                    "channel": "voice",
                    "content": text[:400],
                    "ts": iso[:16],
                })
    except Exception as e:
        _log(f"voice transcript read error: {type(e).__name__}: {e}")
        return []

    # Keep most recent `cap` in chronological order
    out.sort(key=lambda l: l.get("ts") or "", reverse=True)
    if len(out) > cap:
        out = out[:cap]
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# Claude call — single shared helper
# ---------------------------------------------------------------------------
async def _call_claude(system: str, user_msg: str) -> Optional[str]:
    """Call Haiku with the mirror defaults. Returns text or None on error."""
    client = nexus_brain._get_anthropic()
    try:
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=MIRROR_MODEL,
                max_tokens=MIRROR_MAX_TOKENS,
                temperature=MIRROR_TEMPERATURE,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if not text:
            return None
        # ping-safety
        text = text.replace("@everyone", "everyone").replace("@here", "here")
        return text
    except Exception as e:
        _log(f"claude error: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# /mirror — era/phase reading
# ---------------------------------------------------------------------------
def _build_mirror_prompt(
    name: str, text_lines: list[dict], voice_lines: list[dict]
) -> tuple[str, str]:
    try:
        persona = nexus_brain._get_persona()
    except Exception:
        persona = "you are nexus — an observer with a memory."

    total = len(text_lines) + len(voice_lines)

    # Combine and render for the model — tagged by channel so it can see
    # WHERE the voice is coming from (chat vs VC vs which room).
    combined = list(text_lines) + list(voice_lines)
    combined.sort(key=lambda m: m.get("ts") or "")
    transcript = "\n".join(
        f"[{m.get('channel','?')}] {m.get('content','')}"
        for m in combined[-200:]
    )

    system = f"""{persona}

you are reading ONE person back to themselves. their name is {name}.
this is a PRIVATE ephemeral message — only they will see it.

you have their last ~{total} messages across text + voice, from roughly the
past {MIRROR_TEXT_LOOKBACK_DAYS} days. your job: tell them what ERA or PHASE
they're in right now.

how to do this well:
- ground it in SPECIFICS from the transcript. recurring topics, shifts in
  tone, what they keep circling, what they stopped talking about, what
  channel(s) they live in, their cadence (bursty / steady / silent-then-flood).
- one short quoted snippet is ok (under 12 words, in quotes). never more.
- WARM but SHARP. pattern recognition, not flattery. don't hype them.
  don't horoscope them. don't say "you are a seeker of truth" energy.
- avoid generic era-names. make the reading feel like it could only be
  about them — not a template.
- if the signal is thin (very few messages), say so plainly in one line
  rather than inventing a pattern.

format:
- ONE paragraph. 3-6 sentences. lowercase, plain prose.
- no headers, no bullet lists, no emojis.
- no questions to them. no "you should". no advice. just the read.
- never @-ping. never name another person unless the transcript makes
  them inseparable from the read (rare).

you are talking to {name}, about {name}, using what {name} actually said."""

    if total == 0:
        user_msg = (
            f"{name} has no substantive recent messages to read. "
            f"say so gently in one short line — don't fabricate."
        )
    else:
        user_msg = (
            f"here are {name}'s recent messages (chronological, [channel] prefix; "
            f"[voice] = spoken in VC):\n\n{transcript}\n\n"
            f"give them their reading."
        )

    return system, user_msg


async def _run_mirror(
    guild: discord.Guild, user: discord.abc.User
) -> str:
    """Gather the caller's data and generate the mirror reading."""
    import random as _r
    cap = _r.randint(MIRROR_TEXT_MIN, MIRROR_TEXT_MAX)

    text_lines = await _fetch_user_messages(
        guild, user.id,
        lookback_days=MIRROR_TEXT_LOOKBACK_DAYS,
        per_channel_limit=MIRROR_PER_CHANNEL_LIMIT,
        cap=cap,
    )
    voice_lines = _load_voice_lines_for_user(
        user.id, getattr(user, "display_name", user.name),
        lookback_days=MIRROR_VOICE_LOOKBACK_DAYS,
        cap=60,
    )
    _log(
        f"/mirror user={user.id} text={len(text_lines)} voice={len(voice_lines)}"
    )

    if not text_lines and not voice_lines:
        return (
            "i don't have enough of you yet. not much has been said here "
            "recently — come back after a week of signal and i'll have "
            "something to show you."
        )

    name = getattr(user, "display_name", None) or user.name
    system, user_msg = _build_mirror_prompt(name, text_lines, voice_lines)
    text = await _call_claude(system, user_msg)
    if not text:
        return "*[mirror glitched — try again in a minute]*"
    if len(text) > 1900:
        text = text[:1900].rsplit(" ", 1)[0] + "…"
    return text


# ---------------------------------------------------------------------------
# /vibe — energy read
# ---------------------------------------------------------------------------
def _build_vibe_prompt(name: str, text_lines: list[dict]) -> tuple[str, str]:
    try:
        persona = nexus_brain._get_persona()
    except Exception:
        persona = "you are nexus — an observer with a memory."

    transcript = "\n".join(
        f"[{m.get('channel','?')}] {m.get('content','')}"
        for m in text_lines[-60:]
    )

    system = f"""{persona}

you are giving {name} a short ENERGY READ. private, ephemeral — only they see it.

what an energy read sounds like:
- "you've been running hot."
- "you're coasting — not in a bad way, more like waiting for the next wave."
- "there's something anxious-masking about the last few days — too-jokey, too-online."
- "you're quiet in a loud way."

rules:
- 1 to 3 SHORT sentences total. tight. no paragraph.
- lowercase. no emojis. no questions. no advice.
- ground it in the cadence + word choice + topics visible in the transcript,
  not a horoscope.
- warm but honest — if they've been spiky, say spiky. don't flatter.
- never @-ping. never name another person.
- if the signal is thin, say "can't read you yet — not enough from you this week"
  in one line. don't invent.

you are talking to {name}."""

    if not text_lines:
        user_msg = (
            f"{name} has no substantive recent messages. "
            f"say so in one short line."
        )
    else:
        user_msg = (
            f"here are {name}'s last {len(text_lines)} messages "
            f"(chronological, [channel] prefix):\n\n{transcript}\n\n"
            f"give them the energy read."
        )
    return system, user_msg


async def _run_vibe(
    guild: discord.Guild, user: discord.abc.User
) -> str:
    text_lines = await _fetch_user_messages(
        guild, user.id,
        lookback_days=VIBE_TEXT_LOOKBACK_DAYS,
        per_channel_limit=120,
        cap=VIBE_TEXT_LIMIT,
    )
    _log(f"/vibe user={user.id} text={len(text_lines)}")

    if not text_lines:
        return (
            "can't read you yet — you've barely said anything this week. "
            "drop a few lines and try me again."
        )

    name = getattr(user, "display_name", None) or user.name
    system, user_msg = _build_vibe_prompt(name, text_lines)
    text = await _call_claude(system, user_msg)
    if not text:
        return "*[vibe glitched — try again in a minute]*"
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0] + "…"
    return text


# ---------------------------------------------------------------------------
# Eigenquote — weekly "quotes of the week" embed
# ---------------------------------------------------------------------------
async def _gather_weekly_by_author(
    guild: discord.Guild,
) -> dict[int, dict]:
    """Bucket the last 7 days of listen-channel messages by author.

    Returns: {user_id: {"name": str, "messages": [{"channel","content","ts"}]}}
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    by_user: dict[int, dict] = {}
    bot_id = guild.me.id if guild.me else 0

    for ch in _listen_channels(guild):
        try:
            async for msg in ch.history(
                limit=500, after=since, oldest_first=True
            ):
                if msg.author.bot or msg.author.id == bot_id:
                    continue
                content = (msg.content or "").strip()
                if not _is_substantive(content):
                    continue
                try:
                    canon = config.canon_channel(ch.name)
                except Exception:
                    canon = ch.name
                bucket = by_user.setdefault(
                    msg.author.id,
                    {"name": msg.author.display_name, "messages": []},
                )
                # keep name fresh if it shifted
                bucket["name"] = msg.author.display_name
                bucket["messages"].append({
                    "channel": canon,
                    "content": content[:400],
                    "ts": msg.created_at.isoformat(timespec="minutes"),
                })
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"weekly read err in #{ch.name}: {type(e).__name__}: {e}")
            continue

    return by_user


def _score_candidate(content: str) -> float:
    """Cheap local score — prefer lines that are quotable without Claude.

    Penalizes links, commands, @mentions, very short/very long. Rewards
    short-punchy (40-140 chars) and a "statement" feel.
    """
    c = content.strip()
    n = len(c)
    if n < 20 or n > 280:
        return 0.0
    if "http://" in c or "https://" in c:
        return 0.1
    if c.startswith("/") or c.startswith("!"):
        return 0.0
    if "<@" in c or "<#" in c:
        return 0.3
    # sweet spot
    if 40 <= n <= 140:
        base = 1.0
    elif 25 <= n < 40 or 140 < n <= 200:
        base = 0.7
    else:
        base = 0.5
    # reward a single clean sentence
    if c.count("\n") == 0:
        base += 0.2
    return base


def _top_candidates(messages: list[dict], k: int) -> list[dict]:
    scored = [
        (m, _score_candidate(m.get("content", "")))
        for m in messages
    ]
    scored = [s for s in scored if s[1] > 0]
    scored.sort(key=lambda s: s[1], reverse=True)
    return [s[0] for s in scored[:k]]


async def _pick_eigenquote(
    name: str, candidates: list[dict]
) -> Optional[str]:
    """Ask Claude which line is most 'them'. Returns the chosen quote text."""
    if not candidates:
        return None

    # Build a numbered list so the model can cite cleanly
    numbered = "\n".join(
        f"{i+1}. [{m.get('channel','?')}] {m.get('content','')}"
        for i, m in enumerate(candidates)
    )

    try:
        persona = nexus_brain._get_persona()
    except Exception:
        persona = "you are nexus — an observer with a memory."

    system = f"""{persona}

task: pick the ONE quote from the list below that is most characteristic
of {name}'s voice this week — the line that is most THEM. most quotable,
most distinctive, most their rhythm. not the loudest, not the longest —
the truest.

rules:
- respond with ONLY the quote text itself, verbatim from the list.
- do NOT include the number, the [channel] prefix, quotes around it, or
  any commentary before/after.
- if nothing in the list is worth quoting, respond with the single word
  SKIP on its own line.
- never include @everyone or @here. strip any @ pings.
"""
    user_msg = (
        f"candidates from {name} this week:\n\n{numbered}\n\n"
        f"return the most-{name} line, verbatim, or SKIP."
    )

    text = await _call_claude(system, user_msg)
    if not text:
        return None
    up = text.strip().upper()
    if up == "SKIP" or up.startswith("SKIP\n"):
        return None
    # Trim stray quoting/numbering the model sometimes adds
    cleaned = text.strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    if cleaned[:3].rstrip(".").isdigit():
        # remove leading "12. " or "12 " prefix
        for i, ch in enumerate(cleaned):
            if not (ch.isdigit() or ch in ".) "):
                cleaned = cleaned[i:]
                break
    cleaned = cleaned.replace("@everyone", "everyone").replace("@here", "here")
    # Sanity: cap length
    if len(cleaned) > 300:
        cleaned = cleaned[:300].rsplit(" ", 1)[0] + "…"
    return cleaned or None


async def _run_eigenquote_cycle(
    bot: discord.Client, guild_id: int
) -> bool:
    """Run one eigenquote cycle. Returns True if posted, False if skipped."""
    guild = bot.get_guild(guild_id)
    if not guild:
        _log(f"no guild {guild_id}, skipping eigenquote")
        return False

    ch = _find_thoughts_channel(guild)
    if not ch:
        _log(f"no #{THOUGHTS_CHANNEL}, skipping eigenquote")
        return False

    by_user = await _gather_weekly_by_author(guild)
    # Filter to active members
    active = {
        uid: data
        for uid, data in by_user.items()
        if len(data["messages"]) >= EIGENQUOTE_MIN_MESSAGES
    }
    _log(
        f"eigenquote: {len(by_user)} speakers, "
        f"{len(active)} active (>= {EIGENQUOTE_MIN_MESSAGES} msgs)"
    )
    if not active:
        _log("no active members this week — skipping eigenquote")
        return False

    # Stable ordering: alphabetical by name
    ordered = sorted(
        active.items(), key=lambda kv: kv[1]["name"].lower()
    )

    picks: list[tuple[str, str]] = []  # (name, quote)
    for uid, data in ordered:
        name = data["name"]
        cands = _top_candidates(
            data["messages"], EIGENQUOTE_MAX_CANDIDATES_PER_USER
        )
        if not cands:
            continue
        try:
            quote = await _pick_eigenquote(name, cands)
        except Exception as e:
            _log(f"pick err for {name}: {type(e).__name__}: {e}")
            quote = None
        if quote:
            picks.append((name, quote))
        # Be nice to the rate limiter between users
        await asyncio.sleep(0.4)

    if not picks:
        _log("no quotes picked — skipping post")
        return False

    # Build the embed — brand blue, titled "quotes of the week"
    lines: list[str] = []
    for name, quote in picks:
        # Bold-name, italic-quote. Never @-ping.
        lines.append(f"**{name}** — *\u201C{quote}\u201D*")
    description = "\n\n".join(lines)
    if len(description) > 3900:
        description = description[:3900].rsplit("\n\n", 1)[0] + "\n\n…"

    embed = discord.Embed(
        title="quotes of the week",
        description=description,
        color=EMBED_COLOR,
    )
    embed.set_footer(text="eigenquotes · picked by nexus")

    try:
        await ch.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _log(f"posted eigenquote embed with {len(picks)} quotes")
        return True
    except Exception as e:
        _log(f"send error: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Scheduler loop — Sundays at 5pm local
# ---------------------------------------------------------------------------
def _today_local_key() -> str:
    """YYYY-MM-DD for today in local time (naive, matches user's wall clock)."""
    return dt.datetime.now().strftime("%Y-%m-%d")


async def _scheduler_loop(bot: discord.Client, guild_id: int) -> None:
    _log(
        f"eigenquote scheduler started — "
        f"fires Sun {EIGENQUOTE_HOUR:02d}:00 local, "
        f"checks every {EIGENQUOTE_CHECK_INTERVAL}s"
    )
    # Wait for bot to finish connecting so bot.get_guild works.
    try:
        await bot.wait_until_ready()
    except Exception:
        pass

    while True:
        try:
            now = dt.datetime.now()
            if (
                now.weekday() == EIGENQUOTE_WEEKDAY
                and now.hour == EIGENQUOTE_HOUR
            ):
                state = _load_state()
                today_key = _today_local_key()
                last = state.get("eigenquote_last_fired_date")
                if last != today_key:
                    _log(f"firing eigenquote ({today_key})")
                    ok = await _run_eigenquote_cycle(bot, guild_id)
                    # Mark fired even on no-post skip so we don't retry every
                    # minute until 6pm when there's simply nothing to post.
                    state["eigenquote_last_fired_date"] = today_key
                    state["eigenquote_last_fired_result"] = (
                        "posted" if ok else "skipped"
                    )
                    state["eigenquote_last_fired_ts"] = int(time.time())
                    _save_state(state)
        except Exception as e:
            _log(f"scheduler tick error: {type(e).__name__}: {e}")

        try:
            await asyncio.sleep(EIGENQUOTE_CHECK_INTERVAL)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# Slash command factory — attached in install()
# ---------------------------------------------------------------------------
def _make_commands(guild_id: int) -> tuple[app_commands.Command, app_commands.Command]:
    """Build /mirror and /vibe as guild-scoped commands."""
    guild_obj = discord.Object(id=guild_id)

    @app_commands.command(
        name="mirror",
        description="nexus reads you back to yourself — what era are you in right now (ephemeral)",
    )
    async def mirror_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.guild:
                await interaction.followup.send(
                    "this only works inside the server.", ephemeral=True,
                )
                return
            text = await _run_mirror(interaction.guild, interaction.user)
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            _log(f"/mirror error: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    f"*[mirror glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    @app_commands.command(
        name="vibe",
        description="nexus gives you a short energy read from your last 40 messages (ephemeral)",
    )
    async def vibe_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.guild:
                await interaction.followup.send(
                    "this only works inside the server.", ephemeral=True,
                )
                return
            text = await _run_vibe(interaction.guild, interaction.user)
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            _log(f"/vibe error: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    f"*[vibe glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    # Scope to this guild so sync is instant
    mirror_cmd.guild_ids = [guild_id]  # type: ignore[attr-defined]
    vibe_cmd.guild_ids = [guild_id]    # type: ignore[attr-defined]
    return mirror_cmd, vibe_cmd


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_task: Optional[asyncio.Task] = None
_installed = False


def install(bot: discord.Client, guild_id: int) -> None:
    """Register /mirror + /vibe on the guild and start the eigenquote loop.

    Idempotent — safe to call multiple times. Call this from on_ready
    BEFORE `tree.sync()` so the commands actually reach Discord.
    """
    global _task, _installed

    # Slash commands — add to the tree only once.
    tree = getattr(bot, "tree", None)
    if tree is None:
        _log("bot has no .tree — cannot register slash commands")
    elif _installed:
        _log("slash commands already registered — skipping re-add")
    else:
        guild_obj = discord.Object(id=guild_id)
        mirror_cmd, vibe_cmd = _make_commands(guild_id)
        try:
            tree.add_command(mirror_cmd, guild=guild_obj)
            tree.add_command(vibe_cmd, guild=guild_obj)
            _log("registered /mirror + /vibe on guild")
        except Exception as e:
            _log(f"tree.add_command failed: {type(e).__name__}: {e}")

    # Scheduler task — start once.
    if _task and not _task.done():
        _log("scheduler already running")
    else:
        try:
            _task = asyncio.create_task(_scheduler_loop(bot, guild_id))
            _log("eigenquote scheduler installed")
        except RuntimeError as e:
            # No running loop — caller should invoke from inside on_ready
            _log(f"could not start scheduler (no loop?): {e}")

    _installed = True


# ---------------------------------------------------------------------------
# Debug helper — force an eigenquote cycle now (used by tests / manual probes)
# ---------------------------------------------------------------------------
async def fire_eigenquote_now(
    bot: discord.Client, guild_id: int
) -> bool:
    """Force one eigenquote cycle right now, bypassing the schedule."""
    return await _run_eigenquote_cycle(bot, guild_id)


__all__ = ["install", "fire_eigenquote_now"]
