"""
TNC (The Nexus Collective) Discord server bootstrap.

One-shot, idempotent setup script. Re-runnable — skips anything that already exists.

Required env vars:
  DISCORD_BOT_TOKEN   — bot token from Discord Developer Portal
  DISCORD_GUILD_ID    — your TNC server ID (right-click server icon w/ Developer Mode on)

Run:
  pip install -r requirements.txt
  python setup_server.py
"""

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv


# Load .env from this folder (falls back to shell env vars if not present)
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# CONFIG — edit channel/role names here, re-run to apply
# ---------------------------------------------------------------------------

# Roles, top → bottom (Founder is highest, Bots is lowest above @everyone).
# Each entry: (name, color hex, permissions name OR None for default,
#              hoist=show in member sidebar, mentionable)
ROLES = [
    ("Founder",   0x3b82f6, "admin",      True,  True),
    ("Co-pilot",  0x60a5fa, "near_admin", True,  True),
    ("Architect", 0x93c5fd, "mod",        True,  True),
    ("Signal",    0xc7d2fe, None,         True,  True),
    ("Void",      0x4b5563, "void",       False, False),
    ("Bots",      0x2dd4bf, None,         True,  False),
]

# Categories and their channels. Channel kind: "text" or "voice".
# perms_preset: which permission preset to apply (see PERMISSION_PRESETS below).
STRUCTURE = [
    ("🚪 ENTRY", [
        ("first-light",    "text",  "announcement"),
        ("the-charter",    "text",  "announcement"),
        ("new-signal",     "text",  "intros"),
    ]),
    ("🧠 THE NEXUS", [
        ("the-thesis",     "text",  "announcement"),
        ("memory-lab",     "text",  "signal"),
        ("dispatches",     "text",  "signal"),
    ]),
    ("🛠 WORKSHOP", [
        ("geni",           "text",  "signal"),
        ("eft-companion",  "text",  "signal"),
        ("music-lab",      "text",  "signal"),
        ("scrapyard",      "text",  "signal"),
    ]),
    ("💬 THE COMMONS", [
        ("chat",           "text",  "signal"),
        ("tangents",       "text",  "signal"),
        ("dopamine",       "text",  "signal"),
        ("open-mic",       "voice", "signal"),
        ("grind",          "voice", "signal"),
    ]),
    ("🔒 INNER CIRCLE", [
        ("the-table",      "text",  "inner_circle"),
        ("dev-logs",       "text",  "inner_circle"),
    ]),
    ("🤖 MACHINE", [
        ("commands",       "text",  "signal"),
    ]),
]

VISION_CHANNEL = "the-thesis"
VISION_PIN_FILE = Path(__file__).parent / "vision_pin.md"

# ---------------------------------------------------------------------------


def role_perms(preset: str | None) -> discord.Permissions:
    if preset == "admin":
        return discord.Permissions.all()
    if preset == "near_admin":
        # Everything admin has except dangerous server-destruction stuff
        p = discord.Permissions.all()
        p.update(administrator=False, manage_guild=False)
        return p
    if preset == "mod":
        return discord.Permissions(
            manage_messages=True, manage_threads=True,
            kick_members=True, mute_members=True, deafen_members=True,
            move_members=True, view_audit_log=True,
            send_messages=True, read_message_history=True,
            view_channel=True, connect=True, speak=True,
            attach_files=True, embed_links=True, add_reactions=True,
            create_public_threads=True, create_private_threads=True,
        )
    if preset == "void":
        # Read-only base perms; channel-level overwrites further restrict.
        return discord.Permissions(
            view_channel=True, read_message_history=True,
        )
    # Default Signal+
    return discord.Permissions(
        view_channel=True, read_message_history=True,
        send_messages=True, embed_links=True, attach_files=True,
        add_reactions=True, use_external_emojis=True,
        create_public_threads=True, send_messages_in_threads=True,
        connect=True, speak=True, use_voice_activation=True, stream=True,
    )


def channel_overwrites(guild: discord.Guild, preset: str) -> dict:
    everyone = guild.default_role
    void = discord.utils.get(guild.roles, name="Void")
    signal = discord.utils.get(guild.roles, name="Signal")
    architect = discord.utils.get(guild.roles, name="Architect")
    copilot = discord.utils.get(guild.roles, name="Co-pilot")
    founder = discord.utils.get(guild.roles, name="Founder")

    o = {}

    if preset == "announcement":
        # Read-only for everyone (incl. Void). Inner circle can post.
        o[everyone] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        for r in (architect, copilot, founder):
            if r:
                o[r] = discord.PermissionOverwrite(send_messages=True, manage_messages=True)
    elif preset == "intros":
        # Void can read AND post here (only place they can post).
        o[everyone] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        if void:
            o[void] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if signal:
            o[signal] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    elif preset == "signal":
        # Default verified-member channel. Void cannot see.
        o[everyone] = discord.PermissionOverwrite(view_channel=False)
        if void:
            o[void] = discord.PermissionOverwrite(view_channel=False)
        if signal:
            o[signal] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    elif preset == "inner_circle":
        # Architect, Co-pilot, Founder only.
        o[everyone] = discord.PermissionOverwrite(view_channel=False)
        if void:
            o[void] = discord.PermissionOverwrite(view_channel=False)
        if signal:
            o[signal] = discord.PermissionOverwrite(view_channel=False)
        for r in (architect, copilot, founder):
            if r:
                o[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return o


async def ensure_role(guild, name, color, preset, hoist, mentionable):
    existing = discord.utils.get(guild.roles, name=name)
    perms = role_perms(preset)
    if existing:
        # Update if drift
        if (existing.color.value != color or
                existing.hoist != hoist or
                existing.mentionable != mentionable):
            await existing.edit(color=discord.Color(color), hoist=hoist,
                                mentionable=mentionable, permissions=perms)
            print(f"  ~ updated role: {name}")
        else:
            print(f"  = role exists: {name}")
        return existing
    role = await guild.create_role(
        name=name, color=discord.Color(color),
        hoist=hoist, mentionable=mentionable, permissions=perms,
    )
    print(f"  + created role: {name}")
    return role


async def ensure_category(guild, name):
    existing = discord.utils.get(guild.categories, name=name)
    if existing:
        print(f"  = category exists: {name}")
        return existing
    cat = await guild.create_category(name=name)
    print(f"  + created category: {name}")
    return cat


async def ensure_channel(guild, category, name, kind, preset):
    if kind == "text":
        existing = discord.utils.get(category.text_channels, name=name)
    else:
        existing = discord.utils.get(category.voice_channels, name=name)

    overwrites = channel_overwrites(guild, preset)

    if existing:
        # Apply overwrites in case roles/perms drifted
        await existing.edit(overwrites=overwrites)
        print(f"  ~ {kind} channel exists, perms synced: #{name}")
        return existing

    if kind == "text":
        ch = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
    else:
        ch = await guild.create_voice_channel(name=name, category=category, overwrites=overwrites)
    print(f"  + created {kind} channel: #{name}")
    return ch


async def pin_vision(guild):
    if not VISION_PIN_FILE.exists():
        print(f"  ! {VISION_PIN_FILE} not found, skipping vision pin")
        return
    ch = discord.utils.get(guild.text_channels, name=VISION_CHANNEL)
    if not ch:
        print(f"  ! #{VISION_CHANNEL} not found, skipping vision pin")
        return
    content = VISION_PIN_FILE.read_text(encoding="utf-8")
    # Skip if an existing pin from us looks the same
    pins = await ch.pins()
    for p in pins:
        if p.author.id == guild.me.id and p.content.strip()[:80] == content.strip()[:80]:
            print(f"  = vision pin already in #{VISION_CHANNEL}")
            return
    msg = await ch.send(content)
    await msg.pin()
    print(f"  + pinned vision message in #{VISION_CHANNEL}")


async def auto_void_on_join_note(guild):
    # discord.py can't auto-assign roles on join without keeping the bot online.
    # Print instructions so user can configure via Discord's built-in
    # AutoMod / a stay-online bot like Carl-bot or MEE6 if desired.
    print("\n  NOTE: Auto-assigning Void role to new joiners requires either:")
    print("    (a) running this bot continuously with an on_member_join handler, or")
    print("    (b) using a stay-online bot like Carl-bot/MEE6 set to give 'Void' on join.")
    print("    The Phase 2 Nexus bot will handle this; for now, manually assign Void to new members.")


async def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token:
        print("ERROR: set DISCORD_BOT_TOKEN env var")
        sys.exit(1)
    if not guild_id:
        print("ERROR: set DISCORD_GUILD_ID env var")
        sys.exit(1)
    guild_id = int(guild_id)

    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            print(f"\nConnected as {client.user} (id={client.user.id})")
            guild = client.get_guild(guild_id)
            if not guild:
                print(f"ERROR: bot is not in guild {guild_id}. Invite it first.")
                await client.close()
                return

            print(f"Configuring guild: {guild.name}\n")

            print("ROLES")
            for name, color, preset, hoist, mentionable in ROLES:
                await ensure_role(guild, name, color, preset, hoist, mentionable)

            print("\nCATEGORIES + CHANNELS")
            for cat_name, channels in STRUCTURE:
                cat = await ensure_category(guild, cat_name)
                for ch_name, kind, preset in channels:
                    await ensure_channel(guild, cat, ch_name, kind, preset)

            print("\nVISION PIN")
            await pin_vision(guild)

            await auto_void_on_join_note(guild)

            print("\n✓ DONE. Server skeleton applied.")
        except Exception as e:
            print(f"\nERROR during setup: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
