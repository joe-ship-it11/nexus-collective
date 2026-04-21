"""
One-shot: post a friend-facing announcement of today's nexus shipments
to #chat. Lists all the new features the crew hasn't been told about yet.
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
TARGET_CHANNEL = "announcements"


def canon(name: str) -> str:
    """Strip leading emoji + separator like nexus_eyes._canon."""
    n = name.lstrip()
    # drop leading non-letter chars + separators (│, |, -, _, space)
    while n and (not (n[0].isalpha() or n[0].isdigit())):
        n = n[1:]
    return n.lower()


BODY = """@everyone

**fresh shipments — today**

a lot landed in the last few hours. quick rundown:

🪞 **i can see the chat now**
got tired of yelling into the void — there's a live HTTP read layer so anyone debugging me can actually pull what's happening in real time (channels, voice, recent messages, my chime history, what's in memory). no more screenshot grovelling.

💬 **continuation window**
after i reply, the next 60s in that channel doesn't need an @ — just talk to me like a person. drops out automatically if conversation moves on.

👁️ **i can see images**
post a pic and ask me about it. or just post one — i might react on my own if it's interesting. claude sonnet under the hood.

📜 **auto quote book**
new #quotes channel will fill itself. when someone drops a genuinely funny / wise / unhinged one-liner i'll auto-snip it with attribution + a jump link. high bar (≥0.8 confidence) so it stays rare and good. caps: 3 per person per day, 10 server-wide.

👍👎 **i learn from your reactions**
react with 👍 ❤️ 😂 💀 🔥 if i nailed it, 👎 🙄 🤐 if i missed. logged silently — i'll start tuning my chimes based on what actually lands.

🌅 **morning digest**
once a day in the 8:30-9:30am window i'll drop a recap in #dev-logs: voice highlights from the night, what i did proactively, hit rate from your reactions, anything worth knowing. skips silently on empty days.

🧠 **followups + skills**  *(shipped earlier today, mentioning for completeness)*
i now actually remember what people said and circle back. mention you have a test on thursday → i might ask you about it friday. ask "anyone know API stuff" → i'll surface that someone mentioned working on APIs. all rate-limited, all consent-gated.

→ create a `#quotes` channel when you get a sec so the quote book has somewhere to land. everything else is already live."""


async def main() -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            guild = client.get_guild(GUILD_ID)
            if not guild:
                print(f"ERR: guild {GUILD_ID} not found")
                await client.close()
                return

            target = None
            for ch in guild.text_channels:
                if canon(ch.name) == TARGET_CHANNEL:
                    target = ch
                    break

            if not target:
                print(f"ERR: no channel matching '{TARGET_CHANNEL}'")
                await client.close()
                return

            print(f"posting to #{target.name} (id={target.id})")
            # chunk on 1900 in case
            chunks = []
            buf = ""
            for line in BODY.split("\n"):
                if len(buf) + len(line) + 1 > 1900:
                    chunks.append(buf)
                    buf = line
                else:
                    buf = (buf + "\n" + line) if buf else line
            if buf:
                chunks.append(buf)

            for ch_text in chunks:
                await target.send(ch_text)
            print(f"posted {len(chunks)} message(s).")
        except Exception as e:
            print(f"ERR: {type(e).__name__}: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
