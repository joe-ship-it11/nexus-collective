"""
Consent + privacy slash commands for Nexus.

Commands (all ephemeral, user-scoped — anyone can manage their own data):

    /nexus help                  — list every Nexus command + what it does
    /nexus optout                — stop Nexus from remembering anything new about you
    /nexus optin                 — re-enable memory for you
    /nexus mute <minutes>        — pause voice transcription for you for N min
    /nexus quiet <minutes>       — server-wide proactivity mute (stops unprompted chimes)
    /nexus loud                  — lift the proactivity mute immediately
    /nexus shy <on|off>          — per-user opt-out from being chimed-at unprompted
    /nexus forget-all            — NUCLEAR: delete every memory about you (2-step)
    /nexus forget <memory_id>    — delete one specific memory (from /mem list)
    /nexus export                — download every memory Nexus has about you (JSON)

Design notes:
  - All commands are ephemeral (only the caller sees responses)
  - Destructive commands require confirmation via modal button
  - Commands work on the CALLER's data only — you can't forget someone else
  - Opt-out blocks future writes; existing memories are kept until forget-all
"""

from __future__ import annotations

import datetime as dt
import io
import json
import time
from typing import Optional

import discord
from discord import app_commands


# ---------------------------------------------------------------------------
# Confirmation view for destructive actions
# ---------------------------------------------------------------------------
class _ConfirmForgetAll(discord.ui.View):
    def __init__(self, user_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self._user_id = user_id
        self._fired = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only the original user can press these buttons
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "that button isn't yours.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes, forget everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if self._fired:
            return
        self._fired = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_brain
            count = nexus_brain.forget_all_for_user(str(interaction.user.id))
            await interaction.followup.send(
                f"done. forgot **{count}** memories. your profile cache is wiped too.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"*[forget-all glitched: {type(e).__name__}]*",
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self._fired = True
        await interaction.response.send_message(
            "cancelled. nothing deleted.", ephemeral=True,
        )
        self.stop()


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
async def register(tree: app_commands.CommandTree, guild_id: int) -> None:
    """Attach /nexus ... command group to `tree`. Idempotent per-process."""
    if getattr(register, "_registered", False):
        return

    guild_obj = discord.Object(id=guild_id)

    grp = app_commands.Group(
        name="nexus",
        description="your nexus consent + privacy controls",
        guild_ids=[guild_id],
    )

    # -----------------------------------------------------------------------
    # /nexus help
    # -----------------------------------------------------------------------
    @grp.command(name="help", description="what nexus does + every command you can run")
    async def nx_help(interaction: discord.Interaction):
        txt = (
            "**nexus — your group's shared memory, consent-first.**\n\n"
            "i listen to voice + text in this server and remember things so the "
            "group has a shared record. defaults are private — memories are "
            "scoped to you unless you promote them.\n\n"
            "**what you control:**\n"
            "`/nexus optout` — stop me from remembering anything new about you\n"
            "`/nexus optin` — flip memory back on\n"
            "`/nexus mute <min>` — pause voice transcription for you for N minutes\n"
            "`/nexus quiet <min>` — server-wide: stop my unprompted chimes for N min\n"
            "`/nexus loud` — lift the proactivity mute immediately\n"
            "`/nexus shy <on|off>` — stop me chiming *at you* unprompted (you can still @ me)\n"
            "`/nexus forget <id>` — delete one memory (get ids from `/mem list`)\n"
            "`/nexus forget-all` — nuke every memory i have about you\n"
            "`/nexus export` — download everything i know about you (json)\n\n"
            "**memory scopes:**\n"
            "`/mem list` — see your memories with scope + tag + id\n"
            "`/mem scope <id> <scope>` — personal / tnc / public\n\n"
            "**what i DON'T do:** share data outside this server, train on "
            "your stuff, send anything external. ask the server owner if you want the "
            "source code."
        )
        await interaction.response.send_message(txt, ephemeral=True)

    # -----------------------------------------------------------------------
    # /nexus optout
    # -----------------------------------------------------------------------
    @grp.command(name="optout", description="stop nexus from remembering anything new about you")
    async def nx_optout(interaction: discord.Interaction):
        import nexus_consent
        nexus_consent.set_opted_out(interaction.user.id, True)
        await interaction.response.send_message(
            "opted out. i won't record any new messages or voice from you. "
            "existing memories are untouched — use `/nexus forget-all` to "
            "delete those too, or `/nexus optin` to turn memory back on.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /nexus optin
    # -----------------------------------------------------------------------
    @grp.command(name="optin", description="re-enable nexus memory for you")
    async def nx_optin(interaction: discord.Interaction):
        import nexus_consent
        nexus_consent.set_opted_out(interaction.user.id, False)
        await interaction.response.send_message(
            "opted back in. i'll start recording new substantive messages + "
            "voice again.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /nexus mute <minutes>
    # -----------------------------------------------------------------------
    @grp.command(name="mute", description="pause voice transcription for you for N minutes")
    @app_commands.describe(minutes="how long to mute (1-240)")
    async def nx_mute(interaction: discord.Interaction, minutes: int):
        if minutes < 1 or minutes > 240:
            await interaction.response.send_message(
                "pick 1-240 minutes.", ephemeral=True,
            )
            return
        import nexus_consent
        until = nexus_consent.mute_for_minutes(interaction.user.id, float(minutes))
        lift = dt.datetime.fromtimestamp(until).strftime("%H:%M")
        await interaction.response.send_message(
            f"muted for {minutes} min. resumes at **{lift}** local time. "
            f"text messages still record — this only pauses voice.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /nexus quiet <minutes>  — server-wide proactivity mute
    # -----------------------------------------------------------------------
    @grp.command(
        name="quiet",
        description="mute nexus's unprompted chimes for N minutes (server-wide)",
    )
    @app_commands.describe(minutes="how long to shush (1-1440, max 24h)")
    async def nx_quiet(interaction: discord.Interaction, minutes: int):
        if minutes < 1 or minutes > 1440:
            await interaction.response.send_message(
                "pick 1-1440 minutes (24h max).", ephemeral=True,
            )
            return
        import nexus_consent
        until = nexus_consent.quiet_for_minutes(float(minutes))
        lift = dt.datetime.fromtimestamp(until).strftime("%H:%M")
        await interaction.response.send_message(
            f"shushed for {minutes}min — nexus stops chiming until {lift} local. "
            f"(he'll still answer @-mentions and `nexus, ...`.) "
            f"use `/nexus loud` to lift early.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /nexus loud  — clear quiet immediately
    # -----------------------------------------------------------------------
    @grp.command(name="loud", description="lift the proactivity mute immediately")
    async def nx_loud(interaction: discord.Interaction):
        import nexus_consent
        was_quiet = nexus_consent.is_quiet()
        nexus_consent.clear_quiet()
        if was_quiet:
            await interaction.response.send_message(
                "unmuted. nexus may chime in when relevant again.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "wasn't muted.", ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /nexus shy <on|off>  — per-user opt-out from unprompted chimes
    # -----------------------------------------------------------------------
    @grp.command(
        name="shy",
        description="stop nexus from chiming in *at you* unprompted (you can still @-mention him)",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def nx_shy(interaction: discord.Interaction, state: app_commands.Choice[str]):
        import nexus_consent
        on = state.value == "on"
        nexus_consent.set_shy(interaction.user.id, on)
        if on:
            await interaction.response.send_message(
                "shy mode on — nexus won't proactively reply to your messages. "
                "he'll still respond when you address him directly.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "shy mode off — nexus may chime in on your messages when relevant again.",
                ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /nexus forget-all  (NUCLEAR, 2-step confirm)
    # -----------------------------------------------------------------------
    @grp.command(
        name="forget-all",
        description="DELETE every memory about you (not reversible)",
    )
    async def nx_forget_all(interaction: discord.Interaction):
        view = _ConfirmForgetAll(user_id=interaction.user.id)
        await interaction.response.send_message(
            "**this will permanently delete every memory i have about you.** "
            "your opt-out status stays as-is. are you sure?",
            view=view,
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /nexus forget <memory_id>
    # -----------------------------------------------------------------------
    @grp.command(name="forget", description="delete one memory by id (from /mem list)")
    @app_commands.describe(memory_id="the id column from /mem list")
    async def nx_forget(interaction: discord.Interaction, memory_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_brain
            # Verify ownership before deleting
            m = nexus_brain._get_mem0()
            try:
                with nexus_brain._MEM0_LOCK:
                    existing = m.get(memory_id)
            except Exception:
                existing = None
            if not existing:
                await interaction.followup.send(
                    f"couldn't find `{memory_id}`.", ephemeral=True,
                )
                return
            owner = str(
                existing.get("user_id")
                or (existing.get("metadata") or {}).get("user_id")
                or ""
            )
            if owner and owner != str(interaction.user.id):
                await interaction.followup.send(
                    "that memory isn't yours to delete.", ephemeral=True,
                )
                return
            ok = nexus_brain.forget_memory(memory_id)
            await interaction.followup.send(
                "deleted." if ok else "couldn't delete — check logs.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"*[forget glitched: {type(e).__name__}]*", ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /nexus export
    # -----------------------------------------------------------------------
    @grp.command(name="export", description="download every memory nexus has about you (json)")
    async def nx_export(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_brain
            mems = nexus_brain.get_all_for_user(
                str(interaction.user.id),
                viewer_user_id=str(interaction.user.id),
            )
            import nexus_consent
            payload = {
                "user_id": str(interaction.user.id),
                "user_name": interaction.user.display_name,
                "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
                "opted_out": nexus_consent.is_opted_out(interaction.user.id),
                "memory_count": len(mems or []),
                "memories": mems or [],
            }
            blob = json.dumps(payload, indent=2, default=str).encode("utf-8")
            fp = io.BytesIO(blob)
            fp.seek(0)
            file = discord.File(fp, filename=f"nexus_export_{interaction.user.id}.json")
            await interaction.followup.send(
                f"here's everything i know about you — **{len(mems or [])}** memories.",
                file=file,
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"*[export glitched: {type(e).__name__}]*", ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /nexus think  — architect+ debug: force a mind-loop cycle now
    # -----------------------------------------------------------------------
    @grp.command(
        name="think",
        description="force nexus to think now (architect+ only, ephemeral)",
    )
    async def nx_think(interaction: discord.Interaction):
        # Architect+ gate — same role check used by /diag /why /health
        try:
            from nexus_commands_extra import _is_architect_plus, _deny_non_architect
            if not _is_architect_plus(interaction.user):
                await _deny_non_architect(interaction)
                return
        except Exception:
            # Fallback: tighten to guild_permissions.administrator if import fails
            perms = getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)
            if not perms:
                await interaction.response.send_message(
                    "architect+ only, sorry.", ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import nexus_mind
            thought = await nexus_mind.think_now(interaction.client, interaction.guild.id)
            if thought:
                await interaction.followup.send(
                    f"posted to #💭│thoughts:\n> {thought}", ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "nothing generated — no activity to ground a thought in, "
                    "or #💭│thoughts is missing.",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(
                f"*[think glitched: {type(e).__name__}: {e}]*", ephemeral=True,
            )

    tree.add_command(grp, guild=guild_obj)
    register._registered = True

    # Sync so the commands appear immediately
    try:
        await tree.sync(guild=guild_obj)
    except Exception:
        pass
