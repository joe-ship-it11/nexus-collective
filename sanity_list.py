"""Sanity check: list categories, channels, roles, and member count."""
from discord_admin import run_admin


async def action(guild):
    print(f"\n✓ Connected to: {guild.name} (id={guild.id})")
    print(f"  members: {guild.member_count}")
    print(f"  roles ({len(guild.roles)}): " + ", ".join(r.name for r in guild.roles if r.name != "@everyone"))
    print()
    for cat in sorted(guild.categories, key=lambda c: c.position):
        print(f"  {cat.name}")
        for ch in cat.channels:
            kind = "#" if ch.type.name == "text" else "🔊"
            print(f"    {kind} {ch.name}")


if __name__ == "__main__":
    run_admin(action)
