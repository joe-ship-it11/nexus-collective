"""Facelift v2 — add emoji prefixes to remaining channels + dedupe #thoughts."""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path
import discord
from dotenv import load_dotenv

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])


# (current_name, new_name). Matches by exact name OR by canon suffix.
RENAMES: list[tuple[str, str]] = [
    # De-dupe with category 🧠 THE NEXUS
    ("🧠│thoughts",    "💭│thoughts"),
    # Add emojis to bare channels
    ("memory-lab",     "🗄│memory-lab"),
    ("dispatches",     "📡│dispatches"),
    ("geni",           "🧬│geni"),
    ("eft-companion",  "🪖│eft-companion"),
    ("music-lab",      "🎵│music-lab"),
    ("scrapyard",      "🔧│scrapyard"),
    ("chat",           "🗣│chat"),
    ("tangents",       "🌀│tangents"),
    ("wins",           "🏆│wins"),
    ("the-table",      "🪑│the-table"),
    ("open-mic",       "🎙│open-mic"),
    ("grind",          "💪│grind"),
]


def _canon(name: str) -> str:
    for sep in ("│", "・", "｜", "|"):
        if sep in name:
            return name.split(sep, 1)[1].lower()
    return name.lower()


def find_channel(guild, target: str):
    target_canon = _canon(target)
    for ch in list(guild.text_channels) + list(guild.voice_channels):
        if ch.name == target:
            return ch
        if _canon(ch.name) == target_canon:
            return ch
    return None


async def main():
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
            print(f"[facelift2] guild: {guild.name}")

            for old, new in RENAMES:
                ch = find_channel(guild, old)
                if not ch:
                    print(f"  · skip '{old}' — not found")
                    continue
                if ch.name == new:
                    print(f"  · already named '{new}'")
                    continue
                try:
                    await ch.edit(name=new, reason="facelift v2")
                    print(f"  · '{old}' → '{new}'")
                except Exception as e:
                    print(f"  · rename '{old}' failed: {type(e).__name__}: {e}")

            print("[facelift2] done.")
        except Exception as e:
            print(f"[facelift2] ERROR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
