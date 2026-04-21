"""
Shared helper for ad-hoc TNC server admin tasks.

Claude writes small scripts that import `run_admin(action)` from this file.
Each script defines an async `action(guild)` function that mutates the server,
then calls `run_admin(action)` to execute it.

Pattern:

    from discord_admin import run_admin
    import discord

    async def action(guild):
        cat = discord.utils.get(guild.categories, name="💬 THE COMMONS")
        await guild.create_voice_channel("beats-lab", category=cat)
        print("✓ created #beats-lab")

    if __name__ == "__main__":
        run_admin(action)

Requires:
  pip install discord.py python-dotenv

Env vars (loaded from .env in this folder):
  DISCORD_BOT_TOKEN
  DISCORD_GUILD_ID
"""

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv


# Force UTF-8 on stdout/stderr so emoji and unicode (✓, 🧠, etc.) don't crash
# when output is redirected to a file on Windows (default cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Load .env from this folder
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)


def run_admin(action):
    """Connect the admin bot, run `action(guild)`, then disconnect."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        print("ERROR: DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")
        sys.exit(1)
    guild_id = int(guild_id)

    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id)
            if not guild:
                print(f"ERROR: bot is not in guild {guild_id}.")
                return
            await action(guild)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await client.close()

    asyncio.run(client.start(token))


# ---------------------------------------------------------------------------
# Common helpers — Claude calls these from action() functions
# ---------------------------------------------------------------------------


async def find_category(guild, name):
    """Match a category by name (case-sensitive, emoji-aware)."""
    return discord.utils.get(guild.categories, name=name)


async def find_channel(guild, name):
    """Match any channel (text or voice) by name."""
    ch = discord.utils.get(guild.text_channels, name=name)
    if ch:
        return ch
    return discord.utils.get(guild.voice_channels, name=name)


async def find_role(guild, name):
    return discord.utils.get(guild.roles, name=name)


# Permission overwrite presets reused from setup_server.py
def overwrites_signal_only(guild):
    """Signal+ can see/post, Void/default can't."""
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for role_name in ("Void",):
        r = discord.utils.get(guild.roles, name=role_name)
        if r:
            overwrites[r] = discord.PermissionOverwrite(view_channel=False)
    for role_name in ("Signal", "Architect", "Co-pilot", "Founder"):
        r = discord.utils.get(guild.roles, name=role_name)
        if r:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return overwrites


def overwrites_inner_circle(guild):
    """Architect+ only."""
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for role_name in ("Void", "Signal"):
        r = discord.utils.get(guild.roles, name=role_name)
        if r:
            overwrites[r] = discord.PermissionOverwrite(view_channel=False)
    for role_name in ("Architect", "Co-pilot", "Founder"):
        r = discord.utils.get(guild.roles, name=role_name)
        if r:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return overwrites


def overwrites_announcement(guild):
    """Read-only for everyone; Architect+ can post."""
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
    for role_name in ("Architect", "Co-pilot", "Founder"):
        r = discord.utils.get(guild.roles, name=role_name)
        if r:
            overwrites[r] = discord.PermissionOverwrite(send_messages=True, manage_messages=True)
    return overwrites
