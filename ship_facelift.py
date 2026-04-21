"""
One-shot: rename existing channels with emoji prefixes + create #the-mind.

Discord keeps channel IDs stable across renames, so config matching stays
valid as long as we update the canon_channel() helper in config.py.

Run:  python ship_facelift.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

# Force UTF-8 on Windows so emoji prints don't crash
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])


# old name (plain) → new slug (with emoji prefix). Box-drawing │ separator.
# If a new name already exists we just skip (idempotent).
RENAMES: list[tuple[str, str]] = [
    ("first-light", "👋│welcome"),
    ("new-signal", "🪪│intros"),
    ("the-charter", "📜│rules"),
    ("the-thesis", "🎯│goals"),
    ("commons", "💬│chat"),
    ("workshop", "🛠│builds"),
    ("dev-logs", "📝│logs"),
    ("commands", "⚙│commands"),
    ("about-nexus", "📘│about"),
    ("announcements", "📢│announcements"),
]

# Create this if it doesn't exist
MIND_CHANNEL_NAME = "🧠│thoughts"
MIND_TOPIC = "nexus thinks out loud. humans can read, only nexus writes."


def _canon(name: str) -> str:
    """Strip emoji prefix to recover logical name."""
    for sep in ("│", "・", "｜", "|"):
        if sep in name:
            return name.split(sep, 1)[1].lower()
    return name.lower()


def find_by_any_name(guild: discord.Guild, *candidates: str) -> discord.TextChannel | None:
    lows = [c.lower() for c in candidates]
    for ch in guild.text_channels:
        if ch.name.lower() in lows:
            return ch
        if _canon(ch.name) in lows:
            return ch
    return None


async def ensure_mind(guild: discord.Guild) -> discord.TextChannel:
    existing = find_by_any_name(guild, MIND_CHANNEL_NAME, "the-mind")
    if existing:
        if existing.name != MIND_CHANNEL_NAME:
            await existing.edit(name=MIND_CHANNEL_NAME, reason="facelift")
            print(f"  · renamed existing #{existing.name} → {MIND_CHANNEL_NAME}")
        else:
            print(f"  · #{MIND_CHANNEL_NAME} already exists")
        return existing

    everyone = guild.default_role
    me = guild.me
    overwrites = {
        everyone: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=True,
        ),
        me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            manage_channels=True,
        ),
    }
    ch = await guild.create_text_channel(
        name=MIND_CHANNEL_NAME,
        overwrites=overwrites,
        topic=MIND_TOPIC,
        reason="facelift — the-mind",
    )
    print(f"  · created #{MIND_CHANNEL_NAME} (id={ch.id})")
    return ch


async def main():
    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            print(f"[facelift] logged in as {client.user}")
            guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
            print(f"[facelift] guild: {guild.name}")

            # 1. rename existing channels
            print("[facelift] renaming channels...")
            for old, new in RENAMES:
                ch = find_by_any_name(guild, old, new)
                if not ch:
                    print(f"  · skip '{old}' — not found")
                    continue
                if ch.name == new:
                    print(f"  · already named '{new}'")
                    continue
                try:
                    await ch.edit(name=new, reason="facelift")
                    print(f"  · '{ch.name}' → '{new}'")
                except Exception as e:
                    print(f"  · rename '{old}' failed: {type(e).__name__}: {e}")

            # 2. ensure #the-mind
            print("[facelift] ensuring #the-mind...")
            await ensure_mind(guild)

            print("[facelift] done.")
        except Exception as e:
            print(f"[facelift] ERROR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
