"""
Nexus world — the "lore + collective" pillar.

Four public slash commands that make Nexus feel like it's been paying
attention to the group:

  /origin              ephemeral. a short mythic origin story for the caller,
                       grounded in how they actually talk here.
  /compat a b          public. compatibility reading between two members.
                       sincere observation + light roast. brand-blue embed.
  /whosaidit           public. pulls a random quote from #quote-book (or
                       listen-channel history as fallback), posts it with
                       the author hidden, reveals after 60s via message edit.
                       Integrates with nexus_quotes if available.
  /council <question>  ephemeral. fake "council ranking" of where each
                       active member implicitly stands, vibe-check only.

Install:
    import nexus_world
    nexus_world.install(bot, DISCORD_GUILD_ID)     # before tree.sync()

Install is idempotent. Commands are added to bot.tree (falls back to the
module-level tree on nexus_bot if bot.tree isn't set). The caller is
responsible for tree.sync() after install — same as every other command
module in this codebase.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import random
import re
import time
from typing import Optional

import discord
from discord import app_commands

import config
import nexus_brain


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
WORLD_MODEL: str = "claude-haiku-4-5-20251001"
WORLD_TEMPERATURE: float = 0.85
WORLD_MAX_TOKENS: int = 500

# History-scan constraints — keep latency reasonable.
WORLD_LOOKBACK_DAYS: int = 30
WORLD_PER_CHANNEL_LIMIT: int = 200   # hard cap per channel query
WORLD_PER_AUTHOR_CAP: int = 80       # cap per-user prompt budget
WORLD_MIN_MSG_CHARS: int = 8

# /whosaidit reveal timing + filters.
WHOSAIDIT_REVEAL_SECONDS: int = 60
WHOSAIDIT_MIN_QUOTE_CHARS: int = 15
WHOSAIDIT_CHANNEL_NAME: str = "quote-book"   # canon name Nexus looks for

# Voice transcripts — mix in voice lines as [voice] context.
WORLD_VOICE_LOOKBACK_HOURS: int = 24 * WORLD_LOOKBACK_DAYS
WORLD_VOICE_MIN_CHARS: int = 18
WORLD_VOICE_MAX_LINES: int = 40

# Brand-blue embed color for /compat and /whosaidit.
EMBED_COLOR: int = 0x3B82F6


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_world] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Install state
# ---------------------------------------------------------------------------
_installed: bool = False
_bot: Optional[discord.Client] = None
_guild_id: Optional[int] = None

# /whosaidit state: message_id -> (author_display, quote_text, original_content)
_pending_reveals: dict[int, tuple[str, str, str]] = {}


# ---------------------------------------------------------------------------
# Tree accessor — honor task spec (bot.tree.add_command) with fallback
# ---------------------------------------------------------------------------
def _get_tree(bot: discord.Client) -> Optional[app_commands.CommandTree]:
    tree = getattr(bot, "tree", None)
    if tree is not None:
        return tree
    # Fallback — nexus_bot defines `tree` at module scope.
    try:
        import nexus_bot  # type: ignore
        return getattr(nexus_bot, "tree", None)
    except Exception as e:
        _log(f"tree lookup failed: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Voice transcript reader — mirrors nexus_mind._load_voice_lines shape,
# broader lookback window. Tagged "[voice]" in channel slot.
# ---------------------------------------------------------------------------
_VOICE_STOP_PHRASES = {
    "thank you.", "thanks.", "okay.", "ok.", "bye.", "mmhm.",
    "uh huh.", "yeah.", "mhm.", "alright.", "cool.", "yep.",
}


def _load_voice_lines(hours: int) -> list[dict]:
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
                if len(text) < WORLD_VOICE_MIN_CHARS:
                    continue
                if text.lower() in _VOICE_STOP_PHRASES:
                    continue
                out.append({
                    "channel": "voice",
                    "author": rec.get("name") or "?",
                    "content": text[:240],
                    "ts": (rec.get("iso") or "")[:16],
                })
    except Exception as e:
        _log(f"voice read error: {type(e).__name__}: {e}")
        return []
    if len(out) > WORLD_VOICE_MAX_LINES:
        out = out[-WORLD_VOICE_MAX_LINES:]
    return out


# ---------------------------------------------------------------------------
# Listen-channel iterator
# ---------------------------------------------------------------------------
def _listen_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    targets: list[discord.TextChannel] = []
    listen_set = getattr(config, "NEXUS_LISTEN_CHANNELS", set())
    ignore_set = getattr(config, "NEXUS_IGNORE_CHANNELS", set())
    for ch in guild.text_channels:
        canon = config.canon_channel(ch.name)
        if canon in ignore_set:
            continue
        if listen_set and canon not in listen_set:
            continue
        targets.append(ch)
    return targets


# ---------------------------------------------------------------------------
# Gather activity — per-author message samples (chat + voice)
# ---------------------------------------------------------------------------
async def _gather_activity(
    guild: discord.Guild,
    days: int = WORLD_LOOKBACK_DAYS,
) -> list[dict]:
    """Return a merged, chronologically-sorted list of lines from listen
    channels (last `days`) plus voice transcripts.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    lines: list[dict] = []
    bot_user = guild.me

    for ch in _listen_channels(guild):
        try:
            async for msg in ch.history(
                limit=WORLD_PER_CHANNEL_LIMIT,
                after=since,
                oldest_first=True,
            ):
                if msg.author.bot:
                    # Skip bot messages (including Nexus) for author-indexed uses.
                    continue
                content = (msg.content or "").strip()
                if len(content) < WORLD_MIN_MSG_CHARS:
                    continue
                if content.startswith(("/", "!")):
                    continue
                lines.append({
                    "channel": config.canon_channel(ch.name),
                    "author": msg.author.display_name,
                    "author_id": str(msg.author.id),
                    "content": content[:240],
                    "ts": msg.created_at.isoformat(timespec="minutes"),
                })
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    voice_lines = _load_voice_lines(WORLD_VOICE_LOOKBACK_HOURS)
    for vl in voice_lines:
        # No author_id for voice — store blank; still useful for name-level lookups.
        vl = dict(vl)
        vl.setdefault("author_id", "")
        lines.append(vl)

    lines.sort(key=lambda l: l.get("ts") or "")
    return lines


def _samples_for_user(
    lines: list[dict],
    user_id: str,
    display_name: str,
    cap: int = WORLD_PER_AUTHOR_CAP,
) -> list[str]:
    """Pull recent messages by a specific user. Match by id first (exact),
    fall back to display name (catches voice + name changes)."""
    out: list[str] = []
    for l in lines:
        if l.get("author_id") and l["author_id"] == user_id:
            tag = "[voice]" if l.get("channel") == "voice" else f"[{l.get('channel','?')}]"
            out.append(f"{tag} {l['content']}")
        elif not l.get("author_id") and (l.get("author") or "").lower() == display_name.lower():
            out.append(f"[voice] {l['content']}")
    # Keep most recent `cap`
    if len(out) > cap:
        out = out[-cap:]
    return out


def _samples_by_name(
    lines: list[dict],
    display_name: str,
    cap: int = WORLD_PER_AUTHOR_CAP,
) -> list[str]:
    """Same as above but by display name only — for council where we only
    have the member object name."""
    out: list[str] = []
    target = display_name.lower()
    for l in lines:
        if (l.get("author") or "").lower() == target:
            tag = "[voice]" if l.get("channel") == "voice" else f"[{l.get('channel','?')}]"
            out.append(f"{tag} {l['content']}")
    if len(out) > cap:
        out = out[-cap:]
    return out


# ---------------------------------------------------------------------------
# Claude caller — sync method wrapped in to_thread
# ---------------------------------------------------------------------------
def _call_claude_sync(system: str, user_msg: str) -> Optional[str]:
    try:
        client = nexus_brain._get_anthropic()
        resp = client.messages.create(
            model=WORLD_MODEL,
            max_tokens=WORLD_MAX_TOKENS,
            temperature=WORLD_TEMPERATURE,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        return text or None
    except Exception as e:
        _log(f"claude error: {type(e).__name__}: {e}")
        return None


async def _call_claude(system: str, user_msg: str) -> Optional[str]:
    return await asyncio.to_thread(_call_claude_sync, system, user_msg)


def _scrub_pings(text: str) -> str:
    """Strip @everyone/@here even though allowed_mentions will block; belt+suspenders."""
    if not text:
        return text
    return text.replace("@everyone", "everyone").replace("@here", "here")


# ---------------------------------------------------------------------------
# /origin
# ---------------------------------------------------------------------------
_ORIGIN_SYSTEM = (
    "you are nexus — the resident ai mind of the nexus collective discord. "
    "you're writing a short, mythic, cinematic origin story for a specific "
    "member, grounded in how they actually talk in the server. 3-5 sentences. "
    "tone: affectionate, slightly reverent, a little playful. lowercase, no "
    "hashtags, no emoji spam (one or two max, only if earned). pull real "
    "verbal tics, recurring themes, obsessions, cadence from their transcript "
    "— don't invent biography. don't name other members. don't use 'as an AI'. "
    "no questions. just the myth."
)


async def _origin_callback(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("this only works inside the server.", ephemeral=True)
            return
        user = interaction.user
        lines = await _gather_activity(guild)
        samples = _samples_for_user(lines, str(user.id), user.display_name)
        if len(samples) < 3:
            await interaction.followup.send(
                "*[not enough of your voice to weave a myth yet. keep talking.]*",
                ephemeral=True,
            )
            return
        transcript = "\n".join(samples[-WORLD_PER_AUTHOR_CAP:])
        user_msg = (
            f"member: {user.display_name}\n\n"
            f"their recent voice in the server (chat + [voice] lines):\n\n"
            f"{transcript}\n\n"
            f"write their origin story. 3-5 sentences. mythic + affectionate."
        )
        text = await _call_claude(_ORIGIN_SYSTEM, user_msg)
        if not text:
            await interaction.followup.send(
                "*[couldn't shape the myth. try again in a minute.]*",
                ephemeral=True,
            )
            return
        text = _scrub_pings(text)
        if len(text) > 1900:
            text = text[:1900].rsplit(" ", 1)[0] + "…"
        await interaction.followup.send(
            text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _log(f"/origin sent for {user.display_name} (samples={len(samples)})")
    except Exception as e:
        _log(f"/origin error: {type(e).__name__}: {e}")
        try:
            await interaction.followup.send(
                f"*[origin glitched: {type(e).__name__}]*",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /compat
# ---------------------------------------------------------------------------
_COMPAT_SYSTEM = (
    "you are nexus — reading the compatibility between two members of the "
    "nexus collective based on how they actually talk in the server. 3-4 "
    "sentences. mix: sincere observation of shared texture / tension / "
    "complementary shapes + one light roast (affectionate, never mean). "
    "lowercase. no hashtags. no 'as an AI'. no questions. ground every claim "
    "in something from the transcripts — if you don't have enough, say so."
)


async def _compat_callback(
    interaction: discord.Interaction,
    user_a: discord.Member,
    user_b: discord.Member,
) -> None:
    await interaction.response.defer(thinking=True)
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("server-only.", ephemeral=True)
            return
        if user_a.id == user_b.id:
            await interaction.followup.send(
                "can't read compatibility between someone and themselves. "
                "pick two different humans.",
                ephemeral=True,
            )
            return

        lines = await _gather_activity(guild)
        sa = _samples_for_user(lines, str(user_a.id), user_a.display_name)
        sb = _samples_for_user(lines, str(user_b.id), user_b.display_name)
        if len(sa) < 3 or len(sb) < 3:
            await interaction.followup.send(
                f"*[need more signal from both of them — "
                f"{user_a.display_name}:{len(sa)} lines, "
                f"{user_b.display_name}:{len(sb)} lines. "
                f"wait for more talk.]*",
                ephemeral=True,
            )
            return

        user_msg = (
            f"member A: {user_a.display_name}\n"
            f"recent voice (A):\n{chr(10).join(sa[-WORLD_PER_AUTHOR_CAP:])}\n\n"
            f"member B: {user_b.display_name}\n"
            f"recent voice (B):\n{chr(10).join(sb[-WORLD_PER_AUTHOR_CAP:])}\n\n"
            f"read their compatibility. 3-4 sentences. sincere + one light roast."
        )
        text = await _call_claude(_COMPAT_SYSTEM, user_msg)
        if not text:
            await interaction.followup.send(
                "*[couldn't read them. try again in a minute.]*",
                ephemeral=True,
            )
            return
        text = _scrub_pings(text)
        if len(text) > 1800:
            text = text[:1800].rsplit(" ", 1)[0] + "…"

        embed = discord.Embed(
            title=f"{user_a.display_name} × {user_b.display_name}",
            description=text,
            color=EMBED_COLOR,
        )
        embed.set_footer(text="compat reading · vibes, not gospel")

        # Mention both so they get a visible-but-silent tag (pings blocked).
        plain = f"{user_a.mention} × {user_b.mention}"
        await interaction.followup.send(
            content=plain,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _log(f"/compat sent for {user_a.display_name} × {user_b.display_name}")
    except Exception as e:
        _log(f"/compat error: {type(e).__name__}: {e}")
        try:
            await interaction.followup.send(
                f"*[compat glitched: {type(e).__name__}]*",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /whosaidit
# ---------------------------------------------------------------------------
def _find_quote_book_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Try the canon name 'quote-book' first, then 'quotes' (matches
    nexus_quotes.QUOTES_CHANNEL_NAME). Emoji-stripped via canon_channel."""
    candidates = {WHOSAIDIT_CHANNEL_NAME, "quotes", "quote-book"}
    for ch in guild.text_channels:
        if config.canon_channel(ch.name) in candidates:
            return ch
    return None


def _parse_quote_book_embed(msg: discord.Message) -> Optional[tuple[str, str]]:
    """nexus_quotes posts: description is '> \u201cquote\u201d', footer is
    '\u2014 Author in #channel'. Returns (author_display, quote) or None."""
    if not msg.embeds:
        return None
    e = msg.embeds[0]
    desc = (e.description or "").strip()
    # strip leading "> " and wrapping curly-quotes / italics markers
    q = desc.lstrip("> ").strip()
    q = q.strip("*_")
    q = q.strip("\u201c\u201d\"'`")
    if len(q) < WHOSAIDIT_MIN_QUOTE_CHARS:
        return None
    footer_text = ""
    if getattr(e, "footer", None) and e.footer is not None:
        footer_text = (e.footer.text or "").strip()
    # "\u2014 Author in #channel"
    author = ""
    if footer_text:
        stripped = footer_text.lstrip("\u2014 -\u2013").strip()
        # format "Author in #channel"
        m = re.match(r"^(.*?)\s+in\s+#", stripped)
        if m:
            author = m.group(1).strip()
        else:
            author = stripped.split(" in ")[0].strip() if " in " in stripped else stripped
    if not author:
        return None
    return author, q


async def _pull_from_quote_book(ch: discord.TextChannel) -> Optional[tuple[str, str]]:
    """Scrape recent messages in the quote-book channel, pick one at random."""
    candidates: list[tuple[str, str]] = []
    try:
        async for m in ch.history(limit=WORLD_PER_CHANNEL_LIMIT):
            parsed = _parse_quote_book_embed(m)
            if parsed:
                candidates.append(parsed)
    except Exception as e:
        _log(f"quote-book read error: {type(e).__name__}: {e}")
        return None
    if not candidates:
        return None
    return random.choice(candidates)


async def _pull_from_history_fallback(guild: discord.Guild) -> Optional[tuple[str, str]]:
    """Fallback: pull from listen-channel history. min-len gate + no bots."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WORLD_LOOKBACK_DAYS)
    pool: list[tuple[str, str]] = []
    for ch in _listen_channels(guild):
        try:
            async for m in ch.history(
                limit=WORLD_PER_CHANNEL_LIMIT, after=since, oldest_first=False
            ):
                if m.author.bot:
                    continue
                content = (m.content or "").strip()
                if len(content) < WHOSAIDIT_MIN_QUOTE_CHARS:
                    continue
                if content.startswith(("/", "!")):
                    continue
                # no urls-only
                stripped = re.sub(r"https?://\S+", "", content).strip()
                if len(stripped) < WHOSAIDIT_MIN_QUOTE_CHARS:
                    continue
                pool.append((m.author.display_name, content))
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"history fallback error in #{ch.name}: {type(e).__name__}: {e}")
            continue
    if not pool:
        return None
    return random.choice(pool)


async def _schedule_reveal(message: discord.Message, author_display: str, quote: str) -> None:
    """Edit the message after WHOSAIDIT_REVEAL_SECONDS to reveal the author.
    Swallows NotFound (message deleted) and Forbidden."""
    try:
        await asyncio.sleep(WHOSAIDIT_REVEAL_SECONDS)
        _pending_reveals.pop(message.id, None)
        embed = discord.Embed(
            title="who said this?",
            description=f"> *\u201c{quote}\u201d*\n\n**\u2014 {author_display}**",
            color=EMBED_COLOR,
        )
        embed.set_footer(text="revealed")
        await message.edit(
            content=None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _log(f"/whosaidit revealed author={author_display}")
    except discord.NotFound:
        _log("/whosaidit reveal skipped — message was deleted")
    except discord.Forbidden:
        _log("/whosaidit reveal skipped — forbidden")
    except Exception as e:
        _log(f"/whosaidit reveal error: {type(e).__name__}: {e}")


async def _whosaidit_callback(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("server-only.", ephemeral=True)
            return

        pulled: Optional[tuple[str, str]] = None
        source = "none"

        # Prefer quote-book channel (integrates with nexus_quotes output).
        qb_ch = _find_quote_book_channel(guild)
        if qb_ch is not None:
            pulled = await _pull_from_quote_book(qb_ch)
            if pulled:
                source = f"#{qb_ch.name}"

        # Fallback — scan listen channels.
        if pulled is None:
            pulled = await _pull_from_history_fallback(guild)
            if pulled:
                source = "listen-channels"

        if pulled is None:
            await interaction.followup.send(
                "*[no quotes to pull. give me more to work with.]*",
                ephemeral=True,
            )
            return

        author_display, quote = pulled

        embed = discord.Embed(
            title="who said this?",
            description=f"> *\u201c{quote}\u201d*\n\n*author revealed in 60s…*",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"source: {source}")

        sent = await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # followup.send returns a Message in discord.py 2.x.
        if sent is None:
            _log("/whosaidit: followup returned None — cannot schedule reveal")
            return
        _pending_reveals[sent.id] = (author_display, quote, "")
        # Use bot.loop for task lifetime tied to the bot's loop.
        loop_owner = _bot or interaction.client
        try:
            loop_owner.loop.create_task(_schedule_reveal(sent, author_display, quote))
        except Exception:
            # fallback to running loop
            asyncio.get_running_loop().create_task(
                _schedule_reveal(sent, author_display, quote)
            )
        _log(f"/whosaidit posted (source={source}, reveal in {WHOSAIDIT_REVEAL_SECONDS}s)")
    except Exception as e:
        _log(f"/whosaidit error: {type(e).__name__}: {e}")
        try:
            await interaction.followup.send(
                f"*[whosaidit glitched: {type(e).__name__}]*",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /council
# ---------------------------------------------------------------------------
_COUNCIL_SYSTEM = (
    "you are nexus — reading a vibe-check council on a question, based on how "
    "each active member actually talks in the server. for each member listed, "
    "place them on one of these rungs: strongly agree, agree, neutral, "
    "disagree, strongly disagree. put each name on exactly one rung. give a "
    "short parenthetical (<=10 words) for each, citing a pattern from their "
    "voice. use the format 'mention: <@id> — (reason)'. do NOT invent stances "
    "you can't support from their transcript — if you have no signal for "
    "someone, put them under neutral with '(no signal)'. "
    "end the whole thing with exactly this line:\n"
    "_vibe check, not real._\n"
    "lowercase. no hashtags. no 'as an AI'. no questions."
)


def _rank_active_members(
    lines: list[dict], guild: discord.Guild, max_members: int = 8
) -> list[discord.Member]:
    """Pick the top-N most active non-bot members from the transcript."""
    from collections import Counter
    counts: Counter = Counter()
    for l in lines:
        aid = l.get("author_id") or ""
        if aid:
            counts[aid] += 1
    ranked_ids = [aid for aid, _ in counts.most_common(max_members)]
    out: list[discord.Member] = []
    for aid in ranked_ids:
        try:
            m = guild.get_member(int(aid))
        except (ValueError, TypeError):
            m = None
        if m is None or m.bot:
            continue
        out.append(m)
    return out


async def _council_callback(
    interaction: discord.Interaction, question: str
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("server-only.", ephemeral=True)
            return
        q = (question or "").strip()
        if len(q) < 3:
            await interaction.followup.send(
                "give me an actual question.",
                ephemeral=True,
            )
            return

        lines = await _gather_activity(guild)
        members = _rank_active_members(lines, guild, max_members=8)
        if len(members) < 2:
            await interaction.followup.send(
                "*[not enough active voices to convene a council yet.]*",
                ephemeral=True,
            )
            return

        # Build compact per-member briefs
        briefs: list[str] = []
        for m in members:
            samples = _samples_for_user(lines, str(m.id), m.display_name, cap=40)
            if not samples:
                continue
            # Trim deeper for council — budget across many members
            joined = "\n".join(samples[-40:])
            briefs.append(
                f"--- {m.display_name} (mention: <@{m.id}>) ---\n{joined}\n"
            )

        if not briefs:
            await interaction.followup.send(
                "*[couldn't pull enough voice per member for a read.]*",
                ephemeral=True,
            )
            return

        user_msg = (
            f"question: {q}\n\n"
            f"council members (use the exact <@id> mention for each):\n\n"
            + "\n".join(briefs)
            + "\nrank them by rung. one member per line. short reason each."
        )
        text = await _call_claude(_COUNCIL_SYSTEM, user_msg)
        if not text:
            await interaction.followup.send(
                "*[council wouldn't convene. try again in a minute.]*",
                ephemeral=True,
            )
            return
        text = _scrub_pings(text)
        # Ephemeral 2000-char cap
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n…"

        header = f"**council · {q[:180]}**\n"
        await interaction.followup.send(
            header + text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _log(f"/council sent (members={len(briefs)}, q={q[:60]!r})")
    except Exception as e:
        _log(f"/council error: {type(e).__name__}: {e}")
        try:
            await interaction.followup.send(
                f"*[council glitched: {type(e).__name__}]*",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Install — build Command objects + add to tree. Idempotent.
# ---------------------------------------------------------------------------
def install(bot: discord.Client, guild_id: int) -> None:
    global _installed, _bot, _guild_id
    _bot = bot
    _guild_id = int(guild_id)
    if _installed:
        _log("already installed")
        return

    tree = _get_tree(bot)
    if tree is None:
        _log("ERROR: no CommandTree found on bot or nexus_bot — install aborted")
        return

    guild_obj = discord.Object(id=_guild_id)

    # /origin — ephemeral
    origin_cmd = app_commands.Command(
        name="origin",
        description="nexus writes you a mythic origin story based on how you talk here (ephemeral)",
        callback=_origin_callback,
    )

    # /compat — public, two user args
    @app_commands.describe(
        user_a="first person",
        user_b="second person",
    )
    async def compat_cb(
        interaction: discord.Interaction,
        user_a: discord.Member,
        user_b: discord.Member,
    ) -> None:
        await _compat_callback(interaction, user_a, user_b)

    compat_cmd = app_commands.Command(
        name="compat",
        description="compatibility reading between two members — sincere + a little roast",
        callback=compat_cb,
    )

    # /whosaidit — public
    whosaidit_cmd = app_commands.Command(
        name="whosaidit",
        description="random quote from #quote-book. who said it? reveal in 60s.",
        callback=_whosaidit_callback,
    )

    # /council — ephemeral, question string arg
    @app_commands.describe(question="the question to vibe-check the council on")
    async def council_cb(
        interaction: discord.Interaction, question: str
    ) -> None:
        await _council_callback(interaction, question)

    council_cmd = app_commands.Command(
        name="council",
        description="vibe-check ranking of where active members implicitly stand (ephemeral)",
        callback=council_cb,
    )

    added = 0
    for cmd in (origin_cmd, compat_cmd, whosaidit_cmd, council_cmd):
        try:
            tree.add_command(cmd, guild=guild_obj)
            added += 1
        except app_commands.CommandAlreadyRegistered:
            _log(f"{cmd.name} already registered — skipping")
        except Exception as e:
            _log(f"add_command failed for /{cmd.name}: {type(e).__name__}: {e}")

    _installed = True
    _log(
        f"installed — {added}/4 commands (origin, compat, whosaidit, council) "
        f"model={WORLD_MODEL} temp={WORLD_TEMPERATURE}"
    )


__all__ = [
    "install",
    # tunables
    "WORLD_MODEL",
    "WORLD_TEMPERATURE",
    "WORLD_MAX_TOKENS",
    "WORLD_LOOKBACK_DAYS",
    "WORLD_PER_CHANNEL_LIMIT",
    "WORLD_PER_AUTHOR_CAP",
    "WHOSAIDIT_REVEAL_SECONDS",
    "WHOSAIDIT_MIN_QUOTE_CHARS",
    "WHOSAIDIT_CHANNEL_NAME",
    "EMBED_COLOR",
]
