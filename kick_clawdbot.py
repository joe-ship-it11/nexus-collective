"""Find and kick ClawdBot (and any other stray bots that aren't our Nexus)."""

import discord
from discord_admin import run_admin

# Keep these bot display names (case-insensitive). Everything else gets kicked.
KEEP = {"tnc admin", "nexus"}


async def action(guild):
    print(f"guild: {guild.name} ({guild.member_count} members)\n")
    kicked = []
    kept = []
    async for m in guild.fetch_members(limit=100):
        if not m.bot:
            continue
        low = m.display_name.lower()
        if low in KEEP:
            kept.append(m.display_name)
            continue
        try:
            await m.kick(reason="not part of TNC bot set")
            kicked.append(m.display_name)
        except discord.Forbidden:
            print(f"  ! no perms to kick {m.display_name}")
        except Exception as e:
            print(f"  ! {m.display_name}: {type(e).__name__}: {e}")

    print(f"KICKED: {kicked}")
    print(f"KEPT:   {kept}")


if __name__ == "__main__":
    run_admin(action)
