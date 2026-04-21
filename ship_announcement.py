"""
One-shot: create #announcements + #about-nexus, post pin, post announcement.

- #announcements: @everyone read-only, bot writes
- #about-nexus:   @everyone read-only, bot writes, pin the about message
- Idempotent: if a channel exists, reuses it; if pin already present, skips

Run:  python ship_announcement.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

ABOUT_PATH = ROOT / "about_nexus_pin.md"
ANNC_PATH = ROOT / "announcement_consent_ship.md"


def load_pin_body() -> str:
    return ABOUT_PATH.read_text(encoding="utf-8").strip()


def load_announcement_body() -> str:
    raw = ANNC_PATH.read_text(encoding="utf-8")
    # strip the meta header (everything before the first '---' divider)
    if "\n---\n" in raw:
        raw = raw.split("\n---\n", 1)[1]
    return raw.strip()


async def ensure_channel(
    guild: discord.Guild,
    name: str,
    *,
    read_only: bool,
    topic: str | None = None,
) -> discord.TextChannel:
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        print(f"  · #{name} exists (id={existing.id}), reusing")
        return existing

    everyone = guild.default_role
    me = guild.me
    overwrites = {
        everyone: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=not read_only,
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
        name=name,
        overwrites=overwrites,
        topic=topic,
        reason="consent surface ship",
    )
    print(f"  · #{name} created (id={ch.id})")
    return ch


async def chunk_send(channel: discord.TextChannel, body: str) -> list[discord.Message]:
    """Discord caps at 2000 chars. Split on blank lines safely."""
    msgs: list[discord.Message] = []
    if len(body) <= 1900:
        msgs.append(await channel.send(body))
        return msgs
    # split on double-newline paragraphs
    parts = body.split("\n\n")
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 2 > 1900:
            if buf.strip():
                msgs.append(await channel.send(buf.strip()))
            buf = p + "\n\n"
        else:
            buf += p + "\n\n"
    if buf.strip():
        msgs.append(await channel.send(buf.strip()))
    return msgs


async def main():
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            print(f"[ship] logged in as {client.user}")
            guild = client.get_guild(GUILD_ID)
            if guild is None:
                guild = await client.fetch_guild(GUILD_ID)
            print(f"[ship] guild: {guild.name}")

            # 1. create/reuse channels
            print("[ship] ensuring channels...")
            about_ch = await ensure_channel(
                guild, "about-nexus",
                read_only=True,
                topic="what nexus is, what it isn't, and how you control it",
            )
            annc_ch = await ensure_channel(
                guild, "announcements",
                read_only=True,
                topic="ship notes + changes from nexus",
            )

            # 2. post the pin in #about-nexus (skip if already pinned by us)
            pins = await about_ch.pins()
            already = any(
                m.author.id == client.user.id and "about nexus" in (m.content or "").lower()
                for m in pins
            )
            if already:
                print("[ship] pin already in #about-nexus, skipping")
            else:
                print("[ship] posting pin body in #about-nexus...")
                pin_msgs = await chunk_send(about_ch, load_pin_body())
                # pin the first message (Discord pins single messages; keeps thread tidy)
                try:
                    await pin_msgs[0].pin(reason="about-nexus main pin")
                    print(f"[ship]   pinned msg {pin_msgs[0].id}")
                except Exception as e:
                    print(f"[ship]   pin failed: {e}")

            # 3. post the announcement in #announcements
            print("[ship] posting announcement in #announcements...")
            annc_body = load_announcement_body()
            # prefix @everyone ping
            annc_body = f"@everyone\n\n{annc_body}"
            await chunk_send(annc_ch, annc_body)
            print("[ship] done.")
        except Exception as e:
            print(f"[ship] ERROR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
