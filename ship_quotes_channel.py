"""
One-shot: create a #quotes text channel in the TNC guild for the
auto quote book module. Idempotent — reuses if it already exists.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
TARGET_NAME = "quotes"
TOPIC = "auto quote book — Nexus snips genuinely funny / wise / unhinged one-liners here"


def canon(name: str) -> str:
    n = name.lstrip()
    while n and (not (n[0].isalpha() or n[0].isdigit())):
        n = n[1:]
    return n.lower()


async def main() -> None:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            guild = client.get_guild(GUILD_ID)
            if not guild:
                print(f"ERR: guild {GUILD_ID} not found")
                await client.close()
                return

            existing = None
            for ch in guild.text_channels:
                if canon(ch.name) == TARGET_NAME:
                    existing = ch
                    break

            if existing:
                print(f"OK: #{existing.name} already exists (id={existing.id})")
                await client.close()
                return

            new_ch = await guild.create_text_channel(
                name=TARGET_NAME,
                topic=TOPIC,
                reason="Nexus auto quote book home",
            )
            print(f"CREATED: #{new_ch.name} (id={new_ch.id})")
        except Exception as e:
            print(f"ERR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
