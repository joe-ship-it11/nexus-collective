"""
Extra debug / status slash commands for TNC Nexus.

Commands (all ephemeral, Architect+ only):
    /diag    — full state embed pulled from the debug HTTP surface
    /why     — last 5 messages from a user + why nexus did/didn't reply
    /health  — one-line plain-text sanity check

Source of truth: nexus_debug_http on http://127.0.0.1:<DEBUG_HTTP_PORT>/state.
If the debug surface is down, commands degrade gracefully.

Usage:
    import nexus_commands_extra
    await nexus_commands_extra.register(tree, DISCORD_GUILD_ID)

register() is idempotent — calling it more than once is a no-op.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Optional

import discord
from discord import app_commands

import config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEBUG_PORT = int(getattr(config, "DEBUG_HTTP_PORT", 18789))
DEBUG_URL = f"http://127.0.0.1:{DEBUG_PORT}"
_STATE_TIMEOUT = 2.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_architect_plus(user) -> bool:
    """True if user is a Member with one of the Architect+ roles."""
    if not isinstance(user, discord.Member):
        return False
    names = {r.name for r in user.roles}
    return bool(names & config.PROMOTION_REACTORS)


def _fetch_state_blocking(timeout: float = _STATE_TIMEOUT) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"{DEBUG_URL}/state", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


async def _fetch_state() -> Optional[dict]:
    return await asyncio.to_thread(_fetch_state_blocking)


async def _deny_non_architect(interaction: discord.Interaction) -> None:
    msg = "architect+ only, sorry."
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
async def register(tree: app_commands.CommandTree, guild_id: int) -> None:
    """
    Attach /diag /why /health to `tree`, scoped to guild_id, then sync.

    Idempotent — only runs once per process.
    """
    if getattr(register, "_registered", False):
        return

    guild_obj = discord.Object(id=guild_id)

    # -----------------------------------------------------------------------
    # /diag — full state embed
    # -----------------------------------------------------------------------
    @tree.command(
        name="diag",
        description="nexus diagnostic dump (architect+ only, ephemeral)",
        guild=guild_obj,
    )
    async def diag(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not _is_architect_plus(interaction.user):
                await _deny_non_architect(interaction)
                return

            state = await _fetch_state()
            if not state:
                await interaction.followup.send(
                    "debug surface unreachable.",
                    ephemeral=True,
                )
                return

            bot_blob = state.get("bot") or {}
            voice = state.get("voice") or {}
            counts = state.get("counts") or {}
            errors = state.get("recent_errors") or []

            embed = discord.Embed(title="nexus diag", color=0x3b82f6)

            # --- bot
            if bot_blob.get("ready"):
                guilds = bot_blob.get("guilds") or []
                g0 = guilds[0] if guilds else {}
                embed.add_field(
                    name="bot",
                    value=(
                        f"ready ✓ · `{bot_blob.get('user','?')}`\n"
                        f"guild: **{g0.get('name','?')}** · "
                        f"members: {g0.get('members','?')}"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(name="bot", value="not ready", inline=False)

            # --- voice
            if voice.get("connected"):
                dave = voice.get("dave") or {}
                if dave.get("present"):
                    dave_str = (
                        f"ready={dave.get('ready')} · "
                        f"status={dave.get('status','?')}"
                    )
                elif "error" in dave:
                    dave_str = f"err: {dave.get('error')}"
                else:
                    dave_str = "no session"
                members = voice.get("members") or []
                member_str = ", ".join(members) if members else "(none)"
                embed.add_field(
                    name="voice",
                    value=(
                        f"connected · **{voice.get('channel','?')}**\n"
                        f"members: {member_str}\n"
                        f"listening: {voice.get('listening')} · "
                        f"playing: {voice.get('playing')}\n"
                        f"dave: {dave_str}"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(name="voice", value="not connected", inline=False)

            # --- counts
            embed.add_field(
                name="counts",
                value=(
                    f"text msgs seen: **{counts.get('recent_text', 0)}** · "
                    f"triggered: **{counts.get('triggered', 0)}** · "
                    f"errors: **{counts.get('recent_errors', 0)}**"
                ),
                inline=False,
            )

            # --- last 3 errors
            if errors:
                last = errors[-3:]
                lines = []
                for e in last:
                    ts = e.get("ts", "?")
                    m = (e.get("msg", "") or "")[:180]
                    lines.append(f"`{ts}` {m}")
                value = "\n".join(lines)
                if len(value) > 1020:
                    value = value[:1017] + "…"
                embed.add_field(name="last errors", value=value, inline=False)
            else:
                embed.add_field(name="last errors", value="none", inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"*[diag glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # /why — last 5 messages from a specific user, + trigger reasoning
    # -----------------------------------------------------------------------
    @tree.command(
        name="why",
        description="why didn't nexus reply to this user? (architect+ only, ephemeral)",
        guild=guild_obj,
    )
    @app_commands.describe(user="user to inspect — last 5 messages + trigger reasoning")
    async def why(interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not _is_architect_plus(interaction.user):
                await _deny_non_architect(interaction)
                return

            state = await _fetch_state()
            if not state:
                await interaction.followup.send(
                    "debug surface unreachable.",
                    ephemeral=True,
                )
                return

            recent = state.get("recent_text") or []
            target_id = str(user.id)
            hits = [m for m in recent if str(m.get("author_id", "")) == target_id]
            hits = hits[-5:]

            if not hits:
                await interaction.followup.send(
                    f"no recent messages logged for **{user.display_name}** "
                    f"(ring buffer = last 80 msgs overall).",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"why — last {len(hits)} from {user.display_name}",
                color=0x3b82f6,
            )
            for i, m in enumerate(hits, 1):
                ts = m.get("ts", "?")
                ch = m.get("channel", "?")
                content = (m.get("content", "") or "")[:200]
                triggered = "✓" if m.get("triggered") else "✗"
                reason = m.get("reason", "?")
                value = (
                    f"`{ts}` · #{ch} · trig: {triggered}\n"
                    f"> {content if content else '(empty)'}\n"
                    f"reason: *{reason}*"
                )
                if len(value) > 1020:
                    value = value[:1017] + "…"
                embed.add_field(name=f"#{i}", value=value, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"*[why glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # /health — one-line plain text
    # -----------------------------------------------------------------------
    @tree.command(
        name="health",
        description="one-line nexus health (architect+ only, ephemeral)",
        guild=guild_obj,
    )
    async def health(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not _is_architect_plus(interaction.user):
                await _deny_non_architect(interaction)
                return

            state = await _fetch_state()
            if not state:
                await interaction.followup.send(
                    "debug surface unreachable",
                    ephemeral=True,
                )
                return

            bot_blob = state.get("bot") or {}
            voice = state.get("voice") or {}
            counts = state.get("counts") or {}
            errors = state.get("recent_errors") or []

            ready = "✓" if bot_blob.get("ready") else "✗"
            vc_str = voice.get("channel") if voice.get("connected") else "none"
            if not vc_str:
                vc_str = "none"
            seen = counts.get("recent_text", 0)
            trig = counts.get("triggered", 0)
            err_ct = counts.get("recent_errors", 0)
            last_err = "none"
            if errors:
                last_err = (errors[-1].get("msg", "") or "")[:120] or "none"

            line = (
                f"ready {ready} | voice: {vc_str} | "
                f"text msgs last 80: {seen} ({trig} triggered) | "
                f"errors: {err_ct} | last: {last_err}"
            )
            await interaction.followup.send(line, ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"*[health glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # /mem — user-scoped memory management (scope relabel + list)
    # Everyone can use these on THEIR OWN memories.
    # -----------------------------------------------------------------------
    mem_group = app_commands.Group(
        name="mem",
        description="manage what nexus remembers about you",
        guild_ids=[guild_id],
    )

    @mem_group.command(
        name="scope",
        description="change a memory's visibility: personal | tnc | public",
    )
    @app_commands.describe(
        memory_id="the memory id from /mem list",
        new_scope="personal (just you) | tnc (whole group) | public (everyone)",
    )
    @app_commands.choices(new_scope=[
        app_commands.Choice(name="personal", value="personal"),
        app_commands.Choice(name="tnc",      value="tnc"),
        app_commands.Choice(name="public",   value="public"),
    ])
    async def mem_scope(
        interaction: discord.Interaction,
        memory_id: str,
        new_scope: app_commands.Choice[str],
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_brain
            # Guard: verify this memory belongs to the caller before relabeling
            m = nexus_brain._get_mem0()
            existing = None
            try:
                with nexus_brain._MEM0_LOCK:
                    existing = m.get(memory_id)
            except Exception:
                existing = None
            if not existing:
                await interaction.followup.send(
                    f"couldn't find memory `{memory_id}`. use `/mem list` to see yours.",
                    ephemeral=True,
                )
                return

            owner = str(existing.get("user_id") or (existing.get("metadata") or {}).get("user_id") or "")
            if owner and owner != str(interaction.user.id):
                await interaction.followup.send(
                    "that memory isn't yours to relabel.",
                    ephemeral=True,
                )
                return

            ok = nexus_brain.update_scope(memory_id, new_scope.value)
            if ok:
                await interaction.followup.send(
                    f"scope updated → **{new_scope.value}**",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "couldn't update — check logs.", ephemeral=True,
                )
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"*[mem scope glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    @mem_group.command(
        name="list",
        description="list your last 10 memories with ids, scopes, tags",
    )
    async def mem_list(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_brain
            mems = nexus_brain.get_all_for_user(
                str(interaction.user.id),
                viewer_user_id=str(interaction.user.id),
            )
            mems = (mems or [])[-10:]
            if not mems:
                await interaction.followup.send(
                    "no memories on file yet. talk more.", ephemeral=True,
                )
                return

            lines = []
            for m in mems:
                mid = str(m.get("id") or m.get("memory_id") or "?")[:12]
                md = m.get("metadata") or {}
                scope = md.get("scope", "personal")
                tag = md.get("tag", "other")
                text = (m.get("memory") or m.get("text") or "")[:90]
                lines.append(f"`{mid}` · **{scope}** · *{tag}* — {text}")
            body = "\n".join(lines)
            if len(body) > 1900:
                body = body[:1897] + "…"
            await interaction.followup.send(body, ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"*[mem list glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    tree.add_command(mem_group, guild=guild_obj)

    register._registered = True

    # Sync the newly-added commands so they appear in Discord immediately.
    try:
        await tree.sync(guild=guild_obj)
    except Exception:
        # Silent — parent already logged the first sync; don't spam.
        pass
