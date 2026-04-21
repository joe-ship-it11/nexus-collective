"""Pull current server state: members, roles, recent activity in key channels."""

import discord
from discord_admin import run_admin


async def action(guild):
    print(f"=== {guild.name} ({guild.member_count} members) ===\n")

    # Members + roles
    print("MEMBERS:")
    async for m in guild.fetch_members(limit=50):
        roles = [r.name for r in m.roles if r.name != "@everyone"]
        tag = " [BOT]" if m.bot else ""
        print(f"  {m.display_name}{tag}  roles={roles}")
    print()

    # Recent activity in key channels
    for ch_name in ("first-light", "the-charter", "the-thesis", "new-signal", "chat", "commands"):
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            continue
        print(f"--- #{ch_name} (last 10) ---")
        msgs = []
        async for m in ch.history(limit=10):
            msgs.append(m)
        msgs.reverse()
        for m in msgs:
            content = m.content[:200] if m.content else "[embed/empty]"
            tag = " [BOT]" if m.author.bot else ""
            ts = m.created_at.strftime("%H:%M:%S")
            print(f"  [{ts}] {m.author.display_name}{tag}: {content}")
        print()


if __name__ == "__main__":
    run_admin(action)
