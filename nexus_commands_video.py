"""
/watch <url> — youtube link in, summary out.

Public response (everyone in the channel sees it). Rate-limited per user
(3 per rolling hour). If the summary classifies as substantive (tnc/public
scope AND a non-"other" tag), a "💾 save to memory" button is attached so
the caller can opt to persist it. Without a save click, nothing hits mem0.

Anyone who calls /watch must be Signal+ (no Voids — keeps random link spam out).

Wire-up:
    import nexus_commands_video
    await nexus_commands_video.register(tree, DISCORD_GUILD_ID)

register() is idempotent.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

import discord
from discord import app_commands

import config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_HOUR = 3
RATE_WINDOW_S = 60 * 60
SAVE_BUTTON_TIMEOUT_S = 15 * 60  # 15min to click save before button disappears

# Embed look — match nexus aesthetic (deep blue accent)
EMBED_COLOR = 0x3b82f6

# Per-user call timestamps (in-memory, resets on bot restart)
_recent_calls: dict[int, deque[float]] = {}


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
def _rate_limit_check(user_id: int) -> tuple[bool, int]:
    """
    Return (allowed, seconds_until_next_slot).
    seconds_until_next_slot is 0 when allowed.
    """
    now = time.time()
    dq = _recent_calls.setdefault(user_id, deque())
    # Drop entries outside the rolling window
    while dq and now - dq[0] > RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_PER_HOUR:
        wait = int(RATE_WINDOW_S - (now - dq[0])) + 1
        return False, max(wait, 1)
    dq.append(now)
    return True, 0


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
def _is_signal_plus(user) -> bool:
    """Signal, Architect, Co-pilot, Founder. Voids fail. DM users fail."""
    if not isinstance(user, discord.Member):
        return False
    names = {r.name for r in user.roles}
    if config.ROLE_VOID in names and not (names & {
        config.ROLE_SIGNAL, config.ROLE_ARCHITECT,
        config.ROLE_COPILOT, config.ROLE_FOUNDER,
    }):
        return False
    return bool(names & {
        config.ROLE_SIGNAL, config.ROLE_ARCHITECT,
        config.ROLE_COPILOT, config.ROLE_FOUNDER,
    })


# ---------------------------------------------------------------------------
# Save button view
# ---------------------------------------------------------------------------
class _SaveView(discord.ui.View):
    """
    Non-persistent view (timeout-bound). Only the original caller can save.
    On click → mem0 add (scope=tnc tag=media), edit message to remove button.
    """

    def __init__(
        self,
        caller_id: int,
        caller_name: str,
        url: str,
        video_id: str,
        summary: str,
        tag: str,
    ):
        super().__init__(timeout=SAVE_BUTTON_TIMEOUT_S)
        self._caller_id = caller_id
        self._caller_name = caller_name
        self._url = url
        self._video_id = video_id
        self._summary = summary
        self._tag = tag
        self._fired = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._caller_id:
            await interaction.response.send_message(
                "only the person who ran /watch can save this one. "
                "run your own /watch if you want it in your memory.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # Disable the button visually if we still have the message reference
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="💾 save to memory",
        style=discord.ButtonStyle.primary,
        custom_id="nexus_video_save",
    )
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._fired:
            await interaction.response.send_message("already saved.", ephemeral=True)
            return
        self._fired = True
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            import nexus_brain
            m = nexus_brain._get_mem0()
            content = f"watched youtube — {self._url}\n\n{self._summary}"
            # Use the same lock as remember()/recall() so we don't race chroma
            with nexus_brain._MEM0_LOCK:
                m.add(
                    messages=[{"role": "user", "content": content}],
                    user_id=str(self._caller_id),
                    agent_id="nexus",
                    metadata={
                        "user_name": self._caller_name,
                        "channel": "video",
                        "scope": "tnc",
                        "tag": "media",
                        "subtag": self._tag,
                        "source": "youtube",
                        "video_id": self._video_id,
                        "url": self._url,
                    },
                )
        except Exception as e:
            await interaction.followup.send(
                f"*[save glitched: {type(e).__name__}: {e}]*", ephemeral=True,
            )
            self._fired = False  # let them retry
            return

        # Disable the button + update the message
        button.disabled = True
        button.label = f"✓ saved by {self._caller_name}"
        button.style = discord.ButtonStyle.success
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        await interaction.followup.send(
            "saved. anyone in tnc can recall it via cross-user search.",
            ephemeral=True,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------
def _build_embed(
    url: str,
    video_id: str,
    summary: str,
    scope: str,
    tag: str,
    substantive: bool,
    char_count: int,
    lang: str,
    caller_name: str,
    source: str = "youtube-captions",
    title_text: Optional[str] = None,
) -> discord.Embed:
    title = "🎬 nexus watched it"
    e = discord.Embed(
        title=title,
        description=summary,
        color=EMBED_COLOR,
        url=url,
    )
    e.set_thumbnail(url=f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")
    if title_text:
        e.add_field(name="title", value=title_text[:200], inline=False)
    badge = "📌 substantive" if substantive else "💭 ephemeral"
    e.add_field(name="vibe", value=f"{badge} · `{scope}` · `{tag}`", inline=True)
    src_emoji = "🎙️" if source == "whisper" else "📝"
    src_label = "whisper" if source == "whisper" else "captions"
    e.add_field(
        name="transcript",
        value=f"{src_emoji} `{src_label}` · `{char_count:,}` chars · `{lang or '?'}`",
        inline=True,
    )
    e.set_footer(
        text=(
            f"requested by {caller_name} · "
            + ("hit save below to keep this in tnc memory" if substantive
               else "not saving this one — looked too noisy / personal / off-topic")
        )
    )
    return e


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
async def register(tree: app_commands.CommandTree, guild_id: int) -> None:
    """Attach /watch to the tree. Idempotent per process."""
    if getattr(register, "_registered", False):
        return

    guild_obj = discord.Object(id=guild_id)

    @app_commands.command(
        name="watch",
        description="have nexus watch a youtube video and summarize it",
    )
    @app_commands.describe(url="full youtube url (youtu.be / youtube.com / shorts ok)")
    async def watch(interaction: discord.Interaction, url: str):
        # Auth
        if not _is_signal_plus(interaction.user):
            await interaction.response.send_message(
                "signal+ only. ask a founder to promote you.", ephemeral=True,
            )
            return

        # Rate limit
        ok, wait_s = _rate_limit_check(interaction.user.id)
        if not ok:
            mins = max(1, wait_s // 60)
            await interaction.response.send_message(
                f"slow down — you've used your 3 watches this hour. "
                f"next slot opens in ~{mins}min.",
                ephemeral=True,
            )
            return

        # Quick ephemeral ack — frees the interaction immediately so we don't
        # hit the 15min interaction-token ceiling for long whisper runs.
        await interaction.response.send_message(
            f"🎬 watching `{url[:80]}` — i'll drop the recap here when done. "
            f"long videos (10+ min) take a few minutes on cpu whisper.",
            ephemeral=True,
        )

        # Post a public placeholder in the channel. Non-interaction messages
        # have no 15min timeout, so we can edit this freely later.
        channel = interaction.channel
        try:
            placeholder = await channel.send(
                f"🎬 *nexus is watching that video {interaction.user.mention} dropped…*"
            )
        except Exception as e:
            # fall back to ephemeral error on the original interaction
            try:
                await interaction.followup.send(
                    f"*[couldn't post placeholder: {type(e).__name__}: {e}]*",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        # Run the heavy lift in a thread (caption fetch + whisper + claude).
        # This is fire-and-forget wrt the interaction — we edit `placeholder`
        # when done regardless of how long it takes.
        try:
            import nexus_video
            loop = asyncio.get_running_loop()
            report = await loop.run_in_executor(None, nexus_video.analyze, url)
        except Exception as e:
            try:
                await placeholder.edit(
                    content=f"*[watch glitched: {type(e).__name__}: {e}]*"
                )
            except Exception:
                pass
            return

        if not report.get("ok"):
            reason = report.get("reason") or "unknown"
            hint = ""
            if "cap is" in reason.lower():
                hint = " (try a shorter clip — cpu whisper is slow)"
            elif "transcript" in reason.lower() or "captions" in reason.lower():
                hint = " (no captions, or yt blocked the fetch)"
            elif "youtube url" in reason.lower():
                hint = " (give me a real youtube url please)"
            try:
                await placeholder.edit(content=f"couldn't watch this — {reason}{hint}")
            except Exception:
                pass
            return

        embed = _build_embed(
            url=report["url"],
            video_id=report["video_id"],
            summary=report["summary"],
            scope=report["scope"],
            tag=report["tag"],
            substantive=report["substantive"],
            char_count=report["char_count"],
            lang=report.get("transcript_lang", ""),
            caller_name=interaction.user.display_name,
            source=report.get("transcript_source", "youtube-captions"),
            title_text=report.get("title"),
        )

        view = None
        if report["substantive"]:
            view = _SaveView(
                caller_id=interaction.user.id,
                caller_name=interaction.user.display_name,
                url=report["url"],
                video_id=report["video_id"],
                summary=report["summary"],
                tag=report["tag"],
            )

        try:
            if view is not None:
                await placeholder.edit(content=None, embed=embed, view=view)
            else:
                await placeholder.edit(content=None, embed=embed)
        except Exception as e:
            # If edit fails (rare — perms), fall back to a fresh channel.send
            try:
                if view is not None:
                    await channel.send(embed=embed, view=view)
                else:
                    await channel.send(embed=embed)
            except Exception:
                pass

    tree.add_command(watch, guild=guild_obj)
    register._registered = True

    try:
        synced = await tree.sync(guild=guild_obj)
        # caller logs the count; we just trust the call
    except Exception:
        pass
