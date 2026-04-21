"""Inspect: list every channel with its current name + emoji (if any)."""
from __future__ import annotations
import asyncio, os, sys, re
from pathlib import Path
import discord
from dotenv import load_dotenv

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

def first_char(s: str) -> str:
    # Extract leading emoji/symbol (everything before first ASCII letter/digit)
    m = re.match(r"^[^a-z0-9\-_]*", s, re.IGNORECASE)
    return m.group(0) if m else ""

async def main():
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
            print(f"=== {guild.name} ===")
            print("\n-- CATEGORIES --")
            for cat in sorted(guild.categories, key=lambda c: c.position):
                print(f"  [{cat.position}] {cat.name}")
            print("\n-- TEXT CHANNELS --")
            for ch in sorted(guild.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
                cat = ch.category.name if ch.category else "(no cat)"
                prefix = first_char(ch.name)
                print(f"  [{cat:30}] '{ch.name}' (prefix={repr(prefix)})")
            print("\n-- VOICE CHANNELS --")
            for ch in sorted(guild.voice_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
                cat = ch.category.name if ch.category else "(no cat)"
                prefix = first_char(ch.name)
                print(f"  [{cat:30}] '{ch.name}' (prefix={repr(prefix)})")
            # Duplicate prefix detection
            all_prefixes = [first_char(ch.name) for ch in guild.channels if first_char(ch.name)]
            dupes = {p: all_prefixes.count(p) for p in set(all_prefixes) if all_prefixes.count(p) > 1}
            if dupes:
                print(f"\n-- DUPLICATE PREFIXES -- {dupes}")
            else:
                print("\n-- NO DUPLICATE PREFIXES --")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
