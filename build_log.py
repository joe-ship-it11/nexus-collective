"""
Build log — one source of truth for everything shipped in TNC.

Dual-posts:
  1. Appends entry to BUILD_LOG.md (local, always works)
  2. Posts a Nexus-voice embed to #dev-logs (Architect+ channel)

Usage (CLI):
    python build_log.py "shipped /pulse command"
    python build_log.py "fixed new-signal bug" "nexus can now reply when @'d in entry channels"

Usage (import):
    import build_log
    build_log.log("shipped /pulse", details="channel-level snapshot, ephemeral")

The log channel name is config.CHANNEL_DEV_LOGS. If the channel doesn't exist,
the script creates it (Architect+ only) on first run.
"""

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import config
from discord_admin import overwrites_inner_circle

# Force UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

LOG_FILE = Path(__file__).parent / "BUILD_LOG.md"
LOG_CHANNEL = getattr(config, "CHANNEL_DEV_LOGS", "dev-logs")
LOG_CATEGORY = getattr(config, "CATEGORY_INNER_CIRCLE", "🔒 INNER CIRCLE")


def _ts_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _ts_human() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _append_local(title: str, details: str | None) -> None:
    """Append to BUILD_LOG.md. Creates file with header if missing."""
    if not LOG_FILE.exists():
        LOG_FILE.write_text(
            "# TNC Build Log\n\n"
            "Everything shipped in the Nexus Collective, newest first.\n\n"
            "---\n\n",
            encoding="utf-8",
        )
    existing = LOG_FILE.read_text(encoding="utf-8")
    # Insert new entry right after the "---" separator so newest is on top
    entry = f"## {_ts_human()} — {title}\n"
    if details:
        entry += f"\n{details}\n"
    entry += "\n"
    marker = "---\n\n"
    if marker in existing:
        head, _, tail = existing.partition(marker)
        new = head + marker + entry + tail
    else:
        new = existing + "\n" + entry
    LOG_FILE.write_text(new, encoding="utf-8")


async def _ensure_channel(guild: discord.Guild) -> discord.TextChannel:
    """Find or create the logs channel (Architect+ only). Tolerant of emoji prefix."""
    target_low = LOG_CHANNEL.lower()
    for c in guild.text_channels:
        if c.name.lower() == target_low:
            return c
        if config.canon_channel(c.name) == target_low:
            return c
    ch = None
    # Create it under Inner Circle category if present
    category = discord.utils.get(guild.categories, name=LOG_CATEGORY)
    overwrites = overwrites_inner_circle(guild)
    ch = await guild.create_text_channel(
        LOG_CHANNEL,
        category=category,
        overwrites=overwrites,
        topic="Nexus build log — every ship, every iteration. Architect+ only.",
    )
    print(f"+ created #{LOG_CHANNEL}")
    return ch


async def _post_embed(guild: discord.Guild, title: str, details: str | None) -> None:
    ch = await _ensure_channel(guild)
    embed = discord.Embed(
        title=f"◇ {title}",
        description=(details or "")[:4000],
        color=0x3b82f6,
        timestamp=dt.datetime.now(),
    )
    embed.set_footer(text="nexus build log")
    await ch.send(embed=embed)


def log(title: str, details: str | None = None) -> None:
    """
    Log a build entry. Writes BUILD_LOG.md + posts to Discord.
    Safe to call from any script — runs the Discord post in a short-lived client.
    """
    # 1) Local first — never fails
    _append_local(title, details)

    # 2) Discord — best effort
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        print("[build_log] no discord creds — logged locally only")
        return

    async def _main():
        intents = discord.Intents.default()
        intents.members = True
        client = discord.Client(intents=intents)
        done = asyncio.Event()

        @client.event
        async def on_ready():
            try:
                guild = client.get_guild(int(guild_id))
                if not guild:
                    print(f"[build_log] bot not in guild {guild_id}")
                    return
                await _post_embed(guild, title, details)
                print(f"[build_log] posted: {title}")
            except Exception as e:
                print(f"[build_log] post error: {type(e).__name__}: {e}")
            finally:
                done.set()
                await client.close()

        await client.start(token)

    try:
        asyncio.run(_main())
    except Exception as e:
        print(f"[build_log] discord client error: {type(e).__name__}: {e}")


def main():
    if len(sys.argv) < 2:
        print("usage: python build_log.py \"title\" [\"details...\"]")
        sys.exit(1)
    title = sys.argv[1]
    details = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
    log(title, details)


if __name__ == "__main__":
    main()
