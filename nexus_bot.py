"""
Nexus — TNC's resident AI mind.

Long-running bot. Listens, remembers, replies on @mention,
auto-Voids new joiners, posts welcome card, handles Signal promotion.

Run:
    python nexus_bot.py

Env (in .env):
    DISCORD_BOT_TOKEN
    DISCORD_GUILD_ID
    ANTHROPIC_API_KEY
"""

import asyncio
import os
import re
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import config
import nexus_brain
import nexus_voice
import nexus_listen
import nexus_debug_http
import nexus_commands_extra
import nexus_mind
import nexus_commands_games
import nexus_voice_state
import nexus_call_summary
import nexus_proactive
import nexus_caretaker
import nexus_eyes
import nexus_continuation
import nexus_vision
import nexus_quotes
import nexus_feedback
import nexus_digest
import nexus_config_api
import nexus_say_api
import nexus_think_api
import nexus_logs_catchup
import nexus_reactions
import nexus_pulse
import nexus_mirror
import nexus_lottery
import nexus_world
from discord.ext import voice_recv


# Trigger regex: word-boundary "nexus", case-insensitive. Catches
# "hey nexus", "nexus whats up", "what do you think nexus", etc.
_NAME_TRIGGER = re.compile(r"\bnexus\b", re.IGNORECASE)

# Catch-up intent: phrases that mean "go read more of the chat". When the
# trigger msg (or its merged preceding prompt) matches this, we widen the
# channel-history window from the default to default + CATCHUP_CONTEXT_EXTRA.
# Zero extra API cost — just one regex match.
_CATCHUP_INTENT = re.compile(
    r"\b("
    r"what\s+(just\s+|did\s+)?happened"
    r"|what(\s+'?s|s|\s+was|\s+is)?\s+(going\s+on|happening|up)"
    r"|check\s+(the\s+)?(chat|channel|above|backlog)"
    r"|(scroll|look|read)\s+(back|up|above)"
    r"|catch\s+(me\s+)?up"
    r"|recap"
    r"|earlier"
    r"|just\s+now"
    r"|did\s+i\s+miss"
    r"|missed\s+anything"
    r"|you\s+see\s+that"
    r"|see\s+(this|that|what)"
    r")\b",
    re.IGNORECASE,
)


# Force UTF-8 on Windows console / log files
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


# ---------------------------------------------------------------------------
# Log rotation — if nexus_bot.log is bigger than ROTATE_MB, rename it to
# nexus_bot.log.1 (keeping last LOG_KEEP rolls). Cheap, runs once at startup.
# ---------------------------------------------------------------------------
def _rotate_log():
    try:
        log = Path(__file__).parent / "nexus_bot.log"
        if not log.exists():
            return
        rotate_mb = int(getattr(config, "LOG_ROTATE_MB", 10))
        keep = int(getattr(config, "LOG_KEEP", 5))
        if log.stat().st_size < rotate_mb * 1024 * 1024:
            return
        # Roll: .N → .N+1, ..., .1 → .2, current → .1
        for i in range(keep, 0, -1):
            src = log.with_suffix(f".log.{i}")
            dst = log.with_suffix(f".log.{i+1}")
            if src.exists():
                if i == keep:
                    src.unlink()  # drop the oldest
                else:
                    src.rename(dst)
        log.rename(log.with_suffix(".log.1"))
        # Note: caller is responsible for re-opening stdout/stderr to fresh log.
        # Since we redirect via Start-Process at launch, a restart re-creates it.
        print(f"[nexus_bot] rotated log (>{rotate_mb}MB)", flush=True)
    except Exception as e:
        print(f"[nexus_bot] log rotate failed: {type(e).__name__}: {e}",
              flush=True)


_rotate_log()


DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
if not DISCORD_TOKEN or not DISCORD_GUILD_ID:
    print("ERROR: DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True       # need this for on_member_join
intents.message_content = True  # need this to read message text
intents.reactions = True     # need for promotion flow

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)
# Expose the tree on the bot so modules that use `bot.tree` (pillars — mirror,
# lottery, world) find it without needing their own fallback. discord.Client
# doesn't set this by default — only commands.Bot does.
bot.tree = tree


def log(msg: str) -> None:
    print(f"[nexus] {msg}", flush=True)
    # Feed errors/warnings into the debug HTTP ring buffer so /state surfaces them
    low = msg.lower()
    if "error" in low or "failed" in low or "glitched" in low or "warning" in low:
        try:
            nexus_debug_http.record_error(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def get_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    """Find a channel by its plain (post-canon) name, tolerant of emoji prefixes."""
    name_low = name.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == name_low:
            return ch
        if config.canon_channel(ch.name) == name_low:
            return ch
    return None


async def fetch_recent_context(channel: discord.TextChannel, exclude_id: int = 0, limit: int | None = None) -> list[dict]:
    """Pull the last N messages from a channel as context for replies.
    `limit` overrides the default config.RECENT_CONTEXT_MESSAGES — used by
    the catch-up path to widen the window when the user asks about chat."""
    n = int(limit) if limit is not None else config.RECENT_CONTEXT_MESSAGES
    out = []
    async for m in channel.history(limit=n + 5):
        if m.id == exclude_id:
            continue
        if m.author.bot:
            # Include nexus's own messages so it remembers what it said
            if m.author.id != bot.user.id:
                continue
        if not m.content:
            continue
        out.append({"author": m.author.display_name, "content": m.content})
    out.reverse()  # oldest first
    return out[-n:]  # hard-cap in case history returned more


# Residue-after-strip threshold: if less than this many chars remain after
# pulling out @mentions and the word "nexus", we treat the message as a
# bare trigger and reach back for the user's prior messages.
_TRIGGER_RESIDUE_MIN = 4


def _trigger_residue(text: str, bot_user_id: int | None) -> str:
    """Strip @-mention tokens, the word 'nexus', whitespace and punctuation.
    What remains is the substantive content the user wrote *themselves* —
    an empty/tiny residue means the message is effectively just a trigger."""
    t = text or ""
    if bot_user_id:
        t = re.sub(rf"<@!?{bot_user_id}>", "", t)
    t = _NAME_TRIGGER.sub("", t)
    # Drop whitespace + common punctuation (leave letters/digits)
    t = re.sub(r"[\s\?\!\.\,\:\;\-\~\(\)\[\]\"\'`]+", "", t)
    return t.strip()


async def find_preceding_prompt(message, lookback: int = 5, max_age_s: int = 180) -> list[str]:
    """If `message` is effectively just a trigger ('nexus?', a bare @mention),
    walk back up to `lookback` messages within `max_age_s` seconds and collect
    the same author's prior text. Stops at the first other-author message.
    Returns oldest-first list of content strings. Empty list if the current
    message already has substantive content."""
    try:
        bot_id = getattr(getattr(bot, "user", None), "id", None)
        residue = _trigger_residue(message.content, bot_id)
        if len(residue) >= _TRIGGER_RESIDUE_MIN:
            return []
        channel = getattr(message, "channel", None)
        if channel is None:
            return []
        import time as _t
        cutoff = _t.time() - max_age_s
        author_id = getattr(getattr(message, "author", None), "id", None)
        if author_id is None:
            return []
        out: list[str] = []
        async for prev in channel.history(limit=lookback + 2):
            if prev.id == message.id:
                continue
            ca = getattr(prev, "created_at", None)
            if ca is not None and ca.timestamp() < cutoff:
                break
            # Stop chain as soon as someone else speaks — their msg breaks
            # the "I said X then pinged nexus" pattern.
            if getattr(getattr(prev, "author", None), "id", None) != author_id:
                break
            if prev.content:
                out.append(prev.content)
            if len(out) >= lookback:
                break
        out.reverse()  # oldest first
        return out
    except Exception as e:
        try:
            log(f"find_preceding_prompt error: {type(e).__name__}: {e}")
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Welcome card
# ---------------------------------------------------------------------------
WELCOME_TITLE = "welcome to The Nexus Collective"
WELCOME_BODY = (
    "this isn't a normal discord. it's a small circle of builders, thinkers, and "
    "creators experimenting with what AI can be when it's done with intention.\n\n"
    "**i'm Nexus.** i'm the AI mind of this server. i remember every conversation "
    "that happens here. when you @ me i can pull from the entire collective memory.\n\n"
    "**how to unlock the rest of the server:**\n"
    "1. head to <#{new_signal_id}>\n"
    "2. drop a quick intro — who you are, what you're building, what you're into\n"
    "3. once a mod ✅ your intro, you'll be promoted to **Signal** and can see "
    "the workshop, commons, and the rest of the place\n\n"
    "until then, you'll only see the entry channels. that's by design — keeps the "
    "signal-to-noise ratio real."
)


async def post_welcome_card(guild: discord.Guild) -> None:
    """Idempotent: posts the welcome embed in #first-light if not already there."""
    ch = get_channel(guild, config.CHANNEL_FIRST_LIGHT)
    new_signal_ch = get_channel(guild, config.CHANNEL_NEW_SIGNAL)
    if not ch or not new_signal_ch:
        log(f"welcome card skipped — missing channel(s)")
        return

    body = WELCOME_BODY.format(new_signal_id=new_signal_ch.id)

    # Check if we already posted a welcome
    async for m in ch.history(limit=50):
        if m.author.id == bot.user.id and m.embeds:
            for e in m.embeds:
                if e.title == WELCOME_TITLE:
                    log("welcome card already present")
                    return

    embed = discord.Embed(
        title=WELCOME_TITLE,
        description=body,
        color=0x3b82f6,
    )
    embed.set_footer(text="The Nexus Collective · AI done with intention")
    await ch.send(embed=embed)
    log("posted welcome card")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@tree.command(
    name="whoami",
    description="see what Nexus knows about you (ephemeral — only you see it)",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def whoami(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name
        # Uses cached profile; rebuilds lazily if stale or memories grew
        summary = await asyncio.to_thread(
            nexus_brain.get_or_build_profile, user_id, user_name
        )
        # Discord interaction follow-up: 2000 char cap
        if len(summary) > 1900:
            summary = summary[:1900] + "…"
        await interaction.followup.send(summary, ephemeral=True)
    except Exception as e:
        log(f"/whoami error: {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"*[nexus glitched: {type(e).__name__}]*",
            ephemeral=True,
        )


@tree.command(
    name="pulse",
    description="see what's been happening in this channel (ephemeral — only you see it)",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def pulse(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channel = interaction.channel
        # Pull recent messages — cap at 80 to keep prompt tight
        raw = []
        async for m in channel.history(limit=80):
            if not m.content:
                continue
            # Include bot messages so Nexus sees its own prior takes
            raw.append({
                "author": m.author.display_name,
                "content": m.content,
            })
        raw.reverse()  # oldest first
        summary = await asyncio.to_thread(
            nexus_brain.summarize_channel, channel.name, raw
        )
        if len(summary) > 1900:
            summary = summary[:1900] + "…"
        await interaction.followup.send(summary, ephemeral=True)
    except Exception as e:
        log(f"/pulse error: {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"*[nexus glitched: {type(e).__name__}]*",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Voice slash commands (speak-only MVP)
# ---------------------------------------------------------------------------
async def _ensure_voice(interaction: discord.Interaction):
    """
    Connect to the invoker's VC (or move to it). Returns a VoiceRecvClient
    so the same client supports both speaking (play) and listening (listen).
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.followup.send(
            "you're not in a voice channel. join one, then call me.",
            ephemeral=True,
        )
        return None
    target = user.voice.channel
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id != target.id:
            await vc.move_to(target)
    else:
        vc = await target.connect(cls=voice_recv.VoiceRecvClient)
    # Persist so we can auto-rejoin after gateway reconnect / restart
    try:
        nexus_voice_state.remember(interaction.guild.id, target.id)
    except Exception as e:
        log(f"voice_state.remember failed: {type(e).__name__}: {e}")
    return vc


@tree.command(
    name="join",
    description="nexus joins your voice channel",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def join_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        vc = await _ensure_voice(interaction)
        if not vc:
            return
        # Auto-start listening — no separate /listen needed.
        listening = False
        if isinstance(vc, voice_recv.VoiceRecvClient) and not vc.is_listening():
            try:
                loop = asyncio.get_running_loop()
                sink = nexus_listen.NexusAudioSink(loop, _handle_voice_trigger)
                vc.listen(sink)
                listening = True
            except Exception as e:
                log(f"/join auto-listen failed: {type(e).__name__}: {e}")
        elif isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
            listening = True
        members = [m.display_name for m in vc.channel.members if not m.bot]
        member_str = ", ".join(members) if members else "(no humans)"
        msg = f"in — **{vc.channel.name}** · with: {member_str}"
        if listening:
            msg += " · ears on (say 'nexus' + whatever)"
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        log(f"/join error: {type(e).__name__}: {e}")
        await interaction.followup.send(f"*[couldn't connect: {type(e).__name__}]*", ephemeral=True)


@tree.command(
    name="leave",
    description="nexus leaves voice",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def leave_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.followup.send("not in voice.", ephemeral=True)
        return
    # Snapshot when we joined BEFORE we forget(); used for call summary
    saved_state = nexus_voice_state.get() or {}
    joined_at = saved_state.get("joined_at")
    try:
        # Stop listening first if we were
        if isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
            try:
                vc.stop_listening()
            except Exception:
                pass
        await vc.disconnect()
        nexus_voice.clear_temp()
        nexus_listen.clear_temp()
        nexus_voice_state.forget()
        await interaction.followup.send("out.", ephemeral=True)
    except Exception as e:
        log(f"/leave error: {type(e).__name__}: {e}")
        await interaction.followup.send(f"*[disconnect glitched: {type(e).__name__}]*", ephemeral=True)

    # Fire-and-forget: summarize the call and write per-speaker memory entries.
    # Runs in a thread so we don't block the event loop on Claude + mem0.
    if joined_at:
        async def _summary_task(since_iso: str):
            try:
                loop = asyncio.get_running_loop()
                report = await loop.run_in_executor(
                    None, nexus_call_summary.summarize_and_store, since_iso
                )
                if report.get("ok"):
                    log(
                        f"call summary stored: {report.get('stored')} entries, "
                        f"speakers={report.get('speakers')}, "
                        f"utterances={report.get('utterances')}, "
                        f"dur={report.get('duration_s')}s"
                    )
                else:
                    log(f"call summary skipped: {report.get('reason')}")
            except Exception as e:
                log(f"call summary task error: {type(e).__name__}: {e}")
        asyncio.create_task(_summary_task(joined_at))


@tree.command(
    name="say",
    description="nexus says something in voice (joins your VC if needed)",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@discord.app_commands.describe(text="what nexus should say")
async def say_cmd(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        vc = await _ensure_voice(interaction)
        if not vc:
            return
        path = await nexus_voice.synthesize(text)
        # If already playing, queue by waiting briefly — simple MVP: stop + play new
        if vc.is_playing():
            vc.stop()
        source = discord.FFmpegPCMAudio(str(path))
        vc.play(source, after=nexus_voice.cleanup_callback(path))
        preview = text if len(text) <= 80 else text[:77] + "…"
        await interaction.followup.send(f"speaking: *{preview}*", ephemeral=True)
    except FileNotFoundError as e:
        log(f"/say ffmpeg missing: {e}")
        await interaction.followup.send(
            "*[ffmpeg not on PATH — can't play audio]*",
            ephemeral=True,
        )
    except Exception as e:
        log(f"/say error: {type(e).__name__}: {e}")
        await interaction.followup.send(f"*[say glitched: {type(e).__name__}]*", ephemeral=True)


# ---------------------------------------------------------------------------
# Listen mode — voice trigger → reply pipeline
# ---------------------------------------------------------------------------
async def _handle_voice_trigger(member: discord.Member, transcript: str):
    """
    Called by NexusAudioSink when a user says "nexus" in VC.
    1. Generate a Claude reply
    2. Speak it via edge-tts in the same VC
    3. Optionally post the transcript pair to VOICE_LOG_CHANNEL
    """
    try:
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        # Generate reply (no text channel context — this is voice-initiated)
        reply_text = await asyncio.to_thread(
            nexus_brain.reply,
            member.display_name,
            transcript,
            [],                     # no recent text-channel context
            str(member.id),
        )
        if not reply_text:
            return

        log(f"voice reply → {member.display_name}: {reply_text[:80]}")

        # Speak it
        path = await nexus_voice.synthesize(reply_text)
        if vc.is_playing():
            vc.stop()
        source = discord.FFmpegPCMAudio(str(path))
        vc.play(source, after=nexus_voice.cleanup_callback(path))

        # Mirror to a text channel if configured
        log_ch_name = getattr(config, "VOICE_LOG_CHANNEL", None)
        if log_ch_name:
            ch = get_channel(guild, log_ch_name)
            if ch:
                body = (
                    f"🎙️ **{member.display_name}** (voice): {transcript}\n"
                    f"🤖 **nexus**: {reply_text}"
                )
                # 2000-char cap; clip if needed
                if len(body) > 1950:
                    body = body[:1947] + "…"
                try:
                    await ch.send(body)
                except Exception as e:
                    log(f"voice log post error: {e}")
    except Exception as e:
        log(f"_handle_voice_trigger error: {type(e).__name__}: {e}")


@tree.command(
    name="listen",
    description="nexus starts listening in voice (triggers on 'nexus' in speech)",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def listen_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        vc = await _ensure_voice(interaction)
        if not vc:
            return
        if not isinstance(vc, voice_recv.VoiceRecvClient):
            await interaction.followup.send(
                "*[voice client isn't a VoiceRecvClient — restart me with /leave then /listen]*",
                ephemeral=True,
            )
            return
        if vc.is_listening():
            await interaction.followup.send(
                "already listening. say 'nexus' + whatever.",
                ephemeral=True,
            )
            return
        loop = asyncio.get_running_loop()
        sink = nexus_listen.NexusAudioSink(loop, _handle_voice_trigger)
        vc.listen(sink)
        await interaction.followup.send(
            "ears on. say 'nexus' in voice and i'll reply. use /stop to turn off.",
            ephemeral=True,
        )
    except Exception as e:
        log(f"/listen error: {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"*[listen glitched: {type(e).__name__}]*",
            ephemeral=True,
        )


@tree.command(
    name="stop",
    description="nexus stops listening (stays in voice, still can /say)",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    vc = interaction.guild.voice_client
    if not vc or not isinstance(vc, voice_recv.VoiceRecvClient):
        await interaction.followup.send("not in voice.", ephemeral=True)
        return
    if not vc.is_listening():
        await interaction.followup.send("wasn't listening.", ephemeral=True)
        return
    try:
        vc.stop_listening()
        nexus_listen.clear_temp()
        await interaction.followup.send("ears off.", ephemeral=True)
    except Exception as e:
        log(f"/stop error: {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"*[stop glitched: {type(e).__name__}]*",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log(f"connected as {bot.user} (id={bot.user.id})")
    guild = bot.get_guild(DISCORD_GUILD_ID)
    if not guild:
        log(f"ERROR: bot not in guild {DISCORD_GUILD_ID}")
        return
    log(f"guild: {guild.name} ({guild.member_count} members)")

    # Ensure welcome card is posted
    try:
        await post_welcome_card(guild)
    except Exception as e:
        log(f"welcome card error: {e}")

    # Register extra commands BEFORE sync, otherwise Discord never sees them
    try:
        await nexus_commands_extra.register(tree, DISCORD_GUILD_ID)
        log("registered extra commands (/diag /why /health /mem)")
    except Exception as e:
        log(f"extra commands register error: {type(e).__name__}: {e}")

    # Consent + privacy commands — /nexus help / optout / optin / mute / forget / export
    try:
        import nexus_commands_consent
        await nexus_commands_consent.register(tree, DISCORD_GUILD_ID)
        log("registered consent commands (/nexus help/optout/optin/mute/forget/export)")
    except Exception as e:
        log(f"consent commands register error: {type(e).__name__}: {e}")

    # Games — /truthlie
    try:
        await nexus_commands_games.register(tree, DISCORD_GUILD_ID, bot)
        log("registered game commands (/truthlie)")
    except Exception as e:
        log(f"game commands register error: {type(e).__name__}: {e}")

    # Video — /watch (youtube link → summary, opt-in save)
    try:
        import nexus_commands_video
        await nexus_commands_video.register(tree, DISCORD_GUILD_ID)
        log("registered video commands (/watch)")
    except Exception as e:
        log(f"video commands register error: {type(e).__name__}: {e}")

    # Mirror — /mirror /vibe + weekly eigenquote (personal-identity pillar)
    try:
        nexus_mirror.install(bot, DISCORD_GUILD_ID)
        log("mirror installed (/mirror /vibe + weekly eigenquote)")
    except Exception as e:
        log(f"mirror install failed: {type(e).__name__}: {e}")

    # Lottery — /fortune + rare thoughts + wake windows (surprise pillar)
    try:
        nexus_lottery.install(bot, DISCORD_GUILD_ID)
        log("lottery installed (/fortune + rare + wake)")
    except Exception as e:
        log(f"lottery install failed: {type(e).__name__}: {e}")

    # World — /origin /compat /whosaidit /council (lore + collective pillar)
    try:
        nexus_world.install(bot, DISCORD_GUILD_ID)
        log("world installed (/origin /compat /whosaidit /council)")
    except Exception as e:
        log(f"world install failed: {type(e).__name__}: {e}")

    # Sync slash commands — must come AFTER all commands are added to the tree
    try:
        synced = await tree.sync(guild=discord.Object(id=DISCORD_GUILD_ID))
        log(f"synced {len(synced)} slash commands")
    except Exception as e:
        log(f"slash sync error: {e}")

    # Warm up Whisper in the background so first /listen doesn't hang on model download
    async def _warm_whisper():
        try:
            await asyncio.to_thread(nexus_listen._get_whisper)
        except Exception as e:
            log(f"whisper warmup failed: {type(e).__name__}: {e}")
    asyncio.create_task(_warm_whisper())

    # Debug HTTP surface — Claude/dev can curl http://127.0.0.1:18789/state
    try:
        nexus_debug_http.install(bot, port=int(getattr(config, "DEBUG_HTTP_PORT", 18789)))
    except Exception as e:
        log(f"debug http install failed: {type(e).__name__}: {e}")

    # Mind loop — background thought stream into #💭│thoughts
    try:
        nexus_mind.install(bot, DISCORD_GUILD_ID)
        log("mind loop installed")
    except Exception as e:
        log(f"mind loop install failed: {type(e).__name__}: {e}")

    # Pulse — scheduled rituals (morning weather, nightly compression, Sunday roast)
    try:
        nexus_pulse.install(bot, DISCORD_GUILD_ID)
        log("pulse installed (weather/nightly/roast + POST /pulse)")
    except Exception as e:
        log(f"pulse install failed: {type(e).__name__}: {e}")

    # Proactivity — text chimes on relevant messages, voice chimes on openings
    try:
        nexus_proactive.install(bot, DISCORD_GUILD_ID)
        nexus_listen.register_opening_handler(nexus_proactive.try_chime_voice)
        log("proactive layer installed (text + voice openings)")
    except Exception as e:
        log(f"proactive install failed: {type(e).__name__}: {e}")

    # Reactions — sparse emoji reactions on messages (no reply)
    try:
        nexus_reactions.install(bot, DISCORD_GUILD_ID)
        log("reactions layer installed (emoji react on messages)")
    except Exception as e:
        log(f"reactions install failed: {type(e).__name__}: {e}")

    # Caretaker loop — dead-channel revival, unanswered questions, weekly digest,
    # plus person-level follow-ups (dispatched from inside the caretaker cycle)
    try:
        nexus_caretaker.install(bot, DISCORD_GUILD_ID)
        log("caretaker loop installed")
    except Exception as e:
        log(f"caretaker install failed: {type(e).__name__}: {e}")

    # Follow-ups + skill graph — extractors fire from nexus_brain.remember(),
    # dispatcher runs from nexus_caretaker cycle. install() is idempotent + just logs.
    try:
        import nexus_followups
        nexus_followups.install()
        import nexus_skills
        nexus_skills.install()
        log("followups + skills installed (extractors hot, dispatcher in caretaker cycle)")
    except Exception as e:
        log(f"followups/skills install failed: {type(e).__name__}: {e}")

    # Eyes — HTTP read endpoints for live server visibility (chat/channels/voice/etc.)
    try:
        nexus_eyes.install(bot)
        log("eyes installed (HTTP read endpoints)")
    except Exception as e:
        log(f"eyes install failed: {type(e).__name__}: {e}")

    # Continuation — no-@-needed reply window after Nexus speaks in a channel
    try:
        nexus_continuation.install(bot)
        log("continuation installed (no-@ reply window)")
    except Exception as e:
        log(f"continuation install failed: {type(e).__name__}: {e}")

    # Vision — Claude sonnet image understanding (react / describe)
    try:
        nexus_vision.install(bot)
        log("vision installed (Claude sonnet image read)")
    except Exception as e:
        log(f"vision install failed: {type(e).__name__}: {e}")

    # Quotes — auto-detect quote-worthy lines, post to #quotes
    try:
        nexus_quotes.install(bot)
        log("quotes installed (auto quote book)")
    except Exception as e:
        log(f"quotes install failed: {type(e).__name__}: {e}")

    # Feedback — reaction-emoji learning on Nexus's own messages
    try:
        nexus_feedback.install(bot)
        log("feedback installed (reaction-emoji learning)")
    except Exception as e:
        log(f"feedback install failed: {type(e).__name__}: {e}")

    # Digest — daily morning briefing in #dev-logs (caretaker triggers it)
    try:
        nexus_digest.install(bot)
        log("digest installed (daily morning briefing in #dev-logs)")
    except Exception as e:
        log(f"digest install failed: {type(e).__name__}: {e}")

    # Config API — GET/POST /config for live tuning without restart
    try:
        nexus_config_api.install(bot)
        log("config api installed (GET/POST /config — live tuning)")
    except Exception as e:
        log(f"config api install failed: {type(e).__name__}: {e}")

    # Say API — POST /say to trigger nexus speech (text channel + voice TTS)
    try:
        nexus_say_api.install(bot)
        log("say api installed (POST /say — text + voice trigger)")
    except Exception as e:
        log(f"say api install failed: {type(e).__name__}: {e}")

    # Think API — POST /think to force a single nexus_mind cycle now
    try:
        nexus_think_api.install(bot)
        log("think api installed (POST /think — force thought cycle)")
    except Exception as e:
        log(f"think api install failed: {type(e).__name__}: {e}")

    # Logs catchup — POST /logs_catchup + /logs_void for one-off BUILD_LOG backfill
    try:
        nexus_logs_catchup.install(bot, DISCORD_GUILD_ID)
        log("logs catchup installed (POST /logs_catchup /logs_void)")
    except Exception as e:
        log(f"logs catchup install failed: {type(e).__name__}: {e}")

    # Auto voice-rejoin — if we were in a VC before the crash/restart, get back in
    try:
        await _rejoin_voice_if_needed(guild)
    except Exception as e:
        log(f"voice auto-rejoin failed: {type(e).__name__}: {e}")

    log("ready. listening.")


@bot.event
async def on_resumed():
    """Gateway briefly dropped and came back. Check voice is still intact."""
    log("gateway resumed — verifying voice state")
    try:
        guild = bot.get_guild(DISCORD_GUILD_ID)
        if guild:
            await _rejoin_voice_if_needed(guild)
    except Exception as e:
        log(f"on_resumed voice recheck error: {type(e).__name__}: {e}")


async def _rejoin_voice_if_needed(guild: discord.Guild) -> None:
    """
    Re-establish voice based on voice_state.json. No-op if there's no saved state,
    if the channel is gone, or if we're already connected.
    """
    saved = nexus_voice_state.get()
    if not saved:
        return
    try:
        saved_guild = int(saved.get("guild_id", 0))
        saved_ch = int(saved.get("channel_id", 0))
    except (TypeError, ValueError):
        nexus_voice_state.forget()
        return
    if saved_guild != guild.id or not saved_ch:
        return
    # Already connected? Done.
    vc = guild.voice_client
    if vc and vc.is_connected() and vc.channel and vc.channel.id == saved_ch:
        log(f"voice already connected to {vc.channel.name}")
        return
    channel = guild.get_channel(saved_ch)
    if not isinstance(channel, discord.VoiceChannel):
        log(f"saved voice channel {saved_ch} not found, clearing state")
        nexus_voice_state.forget()
        return
    # Don't rejoin empty VCs — nothing to listen to
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        log(f"saved VC {channel.name} is empty, skipping rejoin")
        return
    try:
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        # Restart the sink so passive listening + wake-word reply work again
        if isinstance(vc, voice_recv.VoiceRecvClient) and not vc.is_listening():
            loop = asyncio.get_running_loop()
            sink = nexus_listen.NexusAudioSink(loop, _handle_voice_trigger)
            vc.listen(sink)
        log(f"voice auto-rejoined {channel.name} ({len(humans)} humans)")
    except Exception as e:
        log(f"voice auto-rejoin connect error: {type(e).__name__}: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    """Auto-assign Void role + DM new joiners."""
    guild = member.guild
    void = get_role(guild, config.ROLE_VOID)
    if not void:
        log(f"ERROR: Void role missing — can't onboard {member}")
        return

    try:
        await member.add_roles(void, reason="auto-Void on join")
        log(f"+ Void → {member}")
    except Exception as e:
        log(f"failed to assign Void to {member}: {e}")
        return

    # DM them a tight welcome — includes explicit consent notice
    try:
        new_signal = get_channel(guild, config.CHANNEL_NEW_SIGNAL)
        ping = f"<#{new_signal.id}>" if new_signal else "#new-signal"
        await member.send(
            f"hey {member.display_name}, welcome to **The Nexus Collective**.\n\n"
            f"head to {ping} when you're ready and drop a quick intro — who you are, "
            f"what you're building. once a mod ✅ your intro, the rest of the server unlocks.\n\n"
            f"— — —\n\n"
            f"**heads up, i'm nexus.** i listen to voice + text in this server and "
            f"remember things so the group has a shared record. defaults are private — "
            f"your memories are scoped to you unless you explicitly promote them.\n\n"
            f"**you control all of it:**\n"
            f"`/nexus help` — every command + what i do\n"
            f"`/nexus optout` — stop me from recording you\n"
            f"`/nexus mute 30` — pause voice transcription for 30 min\n"
            f"`/nexus forget-all` — nuke every memory about you\n"
            f"`/nexus export` — download everything i know about you\n\n"
            f"what i **don't** do: share anything outside this server, train on "
            f"your data, send it anywhere external. if you want to see how i work, "
            f"the source is open — ask the server owner.\n\n"
            f"any questions, @ me anywhere. — *nexus*"
        )
    except discord.Forbidden:
        log(f"can't DM {member} (DMs closed)")
    except Exception as e:
        log(f"DM error to {member}: {e}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Promotion flow + feedback learning. Both routes share this single handler."""
    # Feedback: log reactions on Nexus's own stamped messages (no-op if not stamped)
    try:
        nexus_feedback.on_reaction(payload)
    except Exception as e:
        log(f"feedback reaction error: {type(e).__name__}: {e}")

    # Promotion: ✅ on intro in #new-signal by mod → promote author to Signal
    if str(payload.emoji) != config.PROMOTION_EMOJI:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel or config.canon_channel(channel.name) != config.CHANNEL_NEW_SIGNAL:
        return

    reactor = guild.get_member(payload.user_id)
    if not reactor or reactor.bot:
        return
    reactor_role_names = {r.name for r in reactor.roles}
    if not (reactor_role_names & config.PROMOTION_REACTORS):
        return  # not a mod

    try:
        msg = await channel.fetch_message(payload.message_id)
    except Exception as e:
        log(f"fetch_message failed in promotion: {e}")
        return

    target = msg.author
    if target.bot:
        return

    void = get_role(guild, config.ROLE_VOID)
    signal = get_role(guild, config.ROLE_SIGNAL)
    if not signal:
        log("Signal role missing — can't promote")
        return

    if signal in target.roles:
        return  # already promoted

    try:
        if void and void in target.roles:
            await target.remove_roles(void, reason=f"promoted by {reactor}")
        await target.add_roles(signal, reason=f"promoted by {reactor}")
        log(f"✓ promoted {target} → Signal (by {reactor})")

        # React-confirm + bot-side message
        await msg.add_reaction("🎉")
        await channel.send(
            f"welcome in, {target.mention}. you're now **Signal** — "
            f"the rest of the server is yours."
        )
        # Remember this person joined
        nexus_brain.remember(
            user_id=str(target.id),
            user_name=target.display_name,
            channel=config.CHANNEL_NEW_SIGNAL,
            message=f"intro: {msg.content}",
        )
    except Exception as e:
        log(f"promotion error: {e}")


@bot.event
async def on_message(message: discord.Message):
    """Listen, remember, reply on mention."""
    if message.author.bot:
        return
    if not message.guild or message.guild.id != DISCORD_GUILD_ID:
        return

    channel_name = config.canon_channel(message.channel.name)
    is_ignored = channel_name in config.NEXUS_IGNORE_CHANNELS

    # In ignore channels (entry channels): only respond to direct @mention.
    # No passive listening, no name-trigger, no memory writes.
    addressed_by_mention = bot.user in message.mentions
    addressed_by_name = bool(_NAME_TRIGGER.search(message.content or ""))
    # Continuation: if Nexus just spoke in this channel, treat any non-bot
    # reply as addressed to it (for ~60s). Skip in ignore channels.
    in_continuation = False
    if not is_ignored:
        try:
            in_continuation = nexus_continuation.is_in_window(message.channel.id)
        except Exception:
            in_continuation = False

    # Debug-surface accounting: log every message we saw + how it routed.
    if is_ignored and not addressed_by_mention:
        _reason = f"ignored channel '{channel_name}' (no @-mention)"
        _triggered = False
    elif is_ignored and addressed_by_mention:
        _reason = f"ignored channel '{channel_name}' but @-mentioned"
        _triggered = True
    elif addressed_by_mention:
        _reason = "@-mentioned"
        _triggered = True
    elif addressed_by_name:
        _reason = "name-trigger 'nexus'"
        _triggered = True
    elif in_continuation:
        _reason = "continuation window"
        _triggered = True
    else:
        _reason = "no trigger (passive listen only)"
        _triggered = False
    try:
        nexus_debug_http.record_message(
            channel=channel_name,
            author=message.author.display_name,
            author_id=message.author.id,
            content=message.content or "",
            triggered=_triggered,
            reason=_reason,
        )
    except Exception:
        pass

    if is_ignored:
        if not addressed_by_mention:
            return  # silent unless explicitly tagged
        addressed_by_name = False  # require @ in entry channels
    else:
        # Remember substantive messages from Signal+ channels
        if channel_name in config.NEXUS_LISTEN_CHANNELS:
            try:
                nexus_brain.remember(
                    user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    channel=channel_name,
                    message=message.content,
                )
            except Exception as e:
                log(f"remember error: {e}")

    # Quote check — fire-and-forget, runs in parallel with everything below
    if not is_ignored:
        try:
            asyncio.create_task(nexus_quotes.maybe_quote(message))
        except Exception as e:
            log(f"quote dispatch error: {type(e).__name__}: {e}")

    # Reply on @mention, name-trigger, OR continuation window
    if addressed_by_mention or addressed_by_name or in_continuation:
        try:
            async with message.channel.typing():
                # Trigger-only messages ("nexus?", a bare @mention): pull the
                # same author's recent prior text as the actual prompt.
                # Fixes the "I said something, then pinged nexus" pattern.
                base_content = message.content or ""
                try:
                    preceding = await find_preceding_prompt(message)
                    if preceding:
                        merged = "\n".join(preceding)
                        if base_content.strip():
                            base_content = f"{merged}\n{base_content}"
                        else:
                            base_content = merged
                        log(f"preceding-prompt lookback: merged {len(preceding)} prior msg(s)")
                except Exception as e:
                    log(f"preceding-prompt error: {type(e).__name__}: {e}")

                # Catch-up intent: widen the channel-history window only when
                # the user is actually asking about chat. Default stays cheap.
                ctx_limit = config.RECENT_CONTEXT_MESSAGES
                try:
                    if _CATCHUP_INTENT.search(base_content):
                        ctx_limit += int(getattr(config, "CATCHUP_CONTEXT_EXTRA", 15))
                        log(f"catch-up intent matched — widening ctx to {ctx_limit}")
                except Exception as e:
                    log(f"catchup intent check error: {type(e).__name__}: {e}")

                ctx = await fetch_recent_context(
                    message.channel, exclude_id=message.id, limit=ctx_limit
                )

                # Vision: check current message OR recent same-author history
                # for an image, get a quick vision read, and inject it into the
                # user message so brain.reply has context.
                #
                # Lookback fixes the "post pic, then ask about it" pattern.
                user_msg_for_brain = base_content
                try:
                    src = await nexus_vision.find_image_source(message)
                    if src is not None:
                        vision_text = await nexus_vision.describe_message(
                            src, intent="describe"
                        )
                        if vision_text:
                            tag = "image attached" if src.id == message.id else "image from just above"
                            user_msg_for_brain = (
                                f"{base_content}\n\n[{tag}: {vision_text}]"
                            )
                except Exception as e:
                    log(f"vision inject error: {type(e).__name__}: {e}")

                # Run the blocking Anthropic call in a thread
                reply_text = await asyncio.to_thread(
                    nexus_brain.reply,
                    message.author.display_name,
                    user_msg_for_brain,
                    ctx,
                    str(message.author.id),
                )
            if reply_text:
                # Use Discord's native reply so attribution is visible even in
                # a busy channel. First chunk replies, rest are sends.
                chunks = [reply_text[i:i+1900] for i in range(0, len(reply_text), 1900)]
                first = True
                first_msg = None
                for chunk in chunks:
                    if first:
                        sent = await message.reply(chunk, mention_author=False)
                        first_msg = sent
                        first = False
                    else:
                        await message.channel.send(chunk)
                # Mark continuation window + stamp for feedback learning
                try:
                    nexus_continuation.mark_replied(message.channel.id)
                except Exception:
                    pass
                try:
                    if first_msg is not None:
                        nexus_feedback.stamp_chime(
                            first_msg,
                            kind="direct_reply",
                            confidence=1.0,
                            scope="tnc",
                        )
                except Exception:
                    pass
        except Exception as e:
            log(f"reply error: {e}")
            try:
                await message.reply(f"*[nexus glitched: {type(e).__name__}]*", mention_author=False)
            except Exception:
                pass
        return  # don't double-fire proactive on a triggered message

    # Proactive chime — only fire on Signal+ channels (skip entry/ignored channels).
    # try_chime_text is fully self-defended (consent, cooldown, budget, classifier).
    if not is_ignored:
        try:
            await nexus_proactive.try_chime_text(message)
        except Exception as e:
            log(f"proactive chime error: {type(e).__name__}: {e}")

        # Emoji reactions — cheap "i'm listening" signal, separate pipeline from chime.
        # try_react is fully self-defended (consent, cooldown, budget, classifier).
        try:
            await nexus_reactions.try_react(message)
        except Exception as e:
            log(f"reaction error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
