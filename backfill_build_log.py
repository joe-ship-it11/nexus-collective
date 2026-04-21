"""
One-shot: backfill BUILD_LOG.md + post every ship from today's build session.
After running this, use `python build_log.py "title"` for single entries.
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv
import os

import build_log

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


# Ordered oldest-first — BUILD_LOG.md inserts each new entry on top so final
# order in the file matches ship order (newest on top).
ENTRIES = [
    ("Phase 1: Nexus bot shipped",
     "Auto-Void on join, welcome card in #first-light, Signal promotion via ✅ reaction, "
     "Mem0 memory layer, @mention → Claude reply with recent channel context."),
    ("Charter + Thesis posted",
     "Written in-voice. Locked into #the-charter and #the-thesis as pinned embeds."),
    ("Void role permissions audited + locked",
     "Entry channels: view yes, post no (except #new-signal). Everything else: hidden."),
    ("Founder + Architect roles assigned to a member", None),
    ("Single-use 24h invite generated for a new member", None),
    ("Name-trigger shipped",
     "Nexus now replies when someone says 'nexus' in a message — no @ required. "
     "Word-boundary regex, case-insensitive."),
    ("Attribution via message.reply()",
     "First chunk of every reply uses Discord's native reply so attribution is visible in busy channels."),
    ("/whoami slash command",
     "Ephemeral, per-user. Dumps what Nexus remembers about the caller in persona voice."),
    ("Voice MVP shipped — /join /leave /say",
     "Edge-TTS neural voice (en-US-AndrewMultilingualNeural). Nexus joins your VC and speaks on command. "
     "FFmpeg 8.0.1 on PATH, davey lib for DAVE protocol."),
    ("discord.py 2.7 voice gotcha fixed",
     "RuntimeError: davey library needed → `pip install davey` (NOT libdavey). Saved to memory for future."),
    ("#new-signal silence bug fixed",
     "Nexus now replies in entry channels when directly @-mentioned. Passive listening + name-trigger still off there — Void joiners can ask for help without him chiming in unprompted."),
    ("/pulse slash command",
     "Channel-level snapshot. Reads last 80 messages, returns a persona-voice pulse check: "
     "who's active, what's alive, what's unresolved. Ephemeral."),
    ("Silent profile builder",
     "Per-user cached profiles. 6-hour TTL, auto-rebuild when memory grows by 3+ entries. "
     "/whoami reads from cache. reply() injects cached profile for continuity in responses."),
    ("Cross-message threading",
     "reply() now pulls two memory blocks — speaker's own history AND cross-user related threads. "
     "Persona updated: when cross-user threads connect to current conversation, surface them by name. "
     "This is the magic moment TNC was built for."),
    ("Build-log automation (this)",
     "`build_log.py` — appends to BUILD_LOG.md + posts a Nexus-voice embed to #dev-logs. "
     "Every future ship gets logged. CLI: `python build_log.py \"title\" \"details\"`."),
]


async def main():
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = int(os.environ["DISCORD_GUILD_ID"])

    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id)
            if not guild:
                print(f"not in guild {guild_id}")
                return
            ch = await build_log._ensure_channel(guild)
            print(f"posting {len(ENTRIES)} entries to #{ch.name}")
            # Header embed
            header = discord.Embed(
                title="◈ build log — backfill",
                description=(
                    f"everything shipped today ({dt.date.today().isoformat()}). "
                    f"from here on, every ship gets its own entry."
                ),
                color=0x3b82f6,
            )
            header.set_footer(text="nexus build log")
            await ch.send(embed=header)

            for title, details in ENTRIES:
                # Append to local
                build_log._append_local(title, details)
                # Post to Discord
                embed = discord.Embed(
                    title=f"◇ {title}",
                    description=(details or "")[:4000],
                    color=0x3b82f6,
                )
                embed.set_footer(text="nexus build log")
                await ch.send(embed=embed)
                await asyncio.sleep(0.35)  # respect rate limits
                print(f"  ✓ {title}")

            print("backfill done")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
