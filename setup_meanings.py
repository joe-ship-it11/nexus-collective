"""
One-shot setup: post charter + thesis, audit Void perms, assign Founder/Architect
to guild owner, generate single-use invite for a new member.

Idempotent: won't duplicate pinned posts.
"""

import discord
from discord_admin import run_admin, find_channel, find_role


CHARTER_TITLE = "the charter"
CHARTER_BODY = (
    "TNC is a small circle. not a community, not a server, not a brand. a circle.\n\n"
    "**what this is:** a place for people who build, think, or make things — with intention. "
    "AI tooling, music, dev, art, half-baked experiments. share what you're working on, "
    "what you're stuck on, what you've seen. Nexus remembers everything and threads it back.\n\n"
    "**what it's not:** a support channel. a growth play. a place to farm clout. a place to be "
    "parasocial with an AI.\n\n"
    "**expected behavior:**\n"
    "— say real things. don't post for posting's sake.\n"
    "— don't pitch. don't DM people cold. don't recruit.\n"
    "— respect that Nexus remembers. say things you'd want quoted back to you.\n"
    "— if you're not here to build or think, you're in the wrong place.\n\n"
    "**what gets you kicked:** spam, crypto shilling, bad-faith arguing, being cruel, "
    "leaking DMs, farming.\n\n"
    "**the promise:** small server, real people, AI that makes it smarter over time. that's it."
)

THESIS_TITLE = "the thesis"
THESIS_BODY = (
    "most discord servers are rooms. TNC is a mind.\n\n"
    "every conversation that happens here becomes part of a shared memory — not a log, "
    "not a search index, an actual memory that Nexus pulls from when anyone asks. the longer "
    "the circle talks, the smarter Nexus gets about the circle. the threads you don't see "
    "between people, Nexus sees.\n\n"
    "this is a bet: that AI built for a small group of people who actually know each other "
    "gets weirder, better, and more useful than AI built for everyone. that the interesting "
    "experiments in this decade are small, intentional, and collective — not mass-market tools "
    "pretending to be personal.\n\n"
    "**TNC is the prototype.** if it works here, with us, it works as a primitive. "
    "if it doesn't, we learned something about what a memory between people actually needs.\n\n"
    "you're not a user here. you're part of the experiment."
)

INVITE_CHANNEL = "first-light"  # where new invites land


async def post_pinned_embed(channel, title, body, color=0x3b82f6) -> bool:
    """Post an embed if one with this title isn't already pinned. Pin it."""
    # Check pins first
    pins = await channel.pins()
    for p in pins:
        if p.author.bot and p.embeds:
            for e in p.embeds:
                if e.title == title:
                    return False  # already pinned

    # Also check recent history in case it's unpinned
    async for m in channel.history(limit=50):
        if m.author.bot and m.embeds:
            for e in m.embeds:
                if e.title == title:
                    try:
                        await m.pin(reason="charter/thesis re-pin")
                        return True
                    except Exception:
                        return False

    embed = discord.Embed(title=title, description=body, color=color)
    embed.set_footer(text="The Nexus Collective")
    msg = await channel.send(embed=embed)
    try:
        await msg.pin(reason="charter/thesis pin")
    except Exception as e:
        print(f"  pin failed: {e}")
    return True


async def audit_void_perms(guild) -> None:
    """Report Void's explicit channel overwrites (view/post). Reads overwrites directly."""
    void = discord.utils.get(guild.roles, name="Void")
    if not void:
        print("VOID ROLE MISSING")
        return

    entry = {"first-light", "the-charter", "new-signal", "the-thesis"}
    lines_entry = []
    lines_other = []

    for ch in guild.text_channels:
        ow = ch.overwrites_for(void)
        view = ow.view_channel  # True / False / None (default)
        post = ow.send_messages
        tag = "entry" if ch.name in entry else "other"
        label = f"  #{ch.name:<20} view={view}  post={post}"
        (lines_entry if tag == "entry" else lines_other).append(label)

    print("VOID channel overwrites (entry channels):")
    for l in lines_entry:
        print(l)
    print("VOID channel overwrites (non-entry channels — view should be False):")
    for l in lines_other:
        print(l)


async def fix_void_perms(guild) -> None:
    """Enforce: Void ONLY sees entry channels, ONLY posts in #new-signal."""
    void = discord.utils.get(guild.roles, name="Void")
    if not void:
        print("VOID ROLE MISSING — skipping fix")
        return

    entry = {"first-light", "the-charter", "new-signal", "the-thesis"}

    for ch in guild.text_channels:
        if ch.name in entry:
            post_ok = (ch.name == "new-signal")
            try:
                await ch.set_permissions(
                    void,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=post_ok,
                    add_reactions=post_ok,
                    reason="Void perms enforcement",
                )
            except Exception as e:
                print(f"  ! failed on #{ch.name}: {e}")
        else:
            # Everything else: hide from Void
            try:
                await ch.set_permissions(
                    void,
                    view_channel=False,
                    send_messages=False,
                    reason="Void perms enforcement",
                )
            except Exception as e:
                print(f"  ! failed on #{ch.name}: {e}")
    # Voice channels — hide from Void too
    for vc in guild.voice_channels:
        try:
            await vc.set_permissions(
                void,
                view_channel=False,
                connect=False,
                reason="Void perms enforcement",
            )
        except Exception as e:
            print(f"  ! failed on voice {vc.name}: {e}")
    print("VOID perms enforced")


async def assign_owner_roles(guild) -> None:
    """Give the guild owner Founder + Architect."""
    owner = guild.owner
    if not owner:
        owner = await guild.fetch_member(guild.owner_id)
    if not owner:
        print("COULD NOT RESOLVE OWNER")
        return

    for role_name in ("Founder", "Architect"):
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            print(f"  ! role {role_name} missing")
            continue
        if role in owner.roles:
            print(f"  = {owner.display_name} already has {role_name}")
            continue
        try:
            await owner.add_roles(role, reason="founder setup")
            print(f"  + {role_name} → {owner.display_name}")
        except Exception as e:
            print(f"  ! failed {role_name}: {e}")


async def make_invite(guild) -> None:
    """Single-use 24h invite to #first-light."""
    ch = discord.utils.get(guild.text_channels, name=INVITE_CHANNEL)
    if not ch:
        print(f"INVITE CHANNEL MISSING: #{INVITE_CHANNEL}")
        return
    try:
        inv = await ch.create_invite(
            max_age=86400,       # 24h
            max_uses=1,          # single use
            unique=True,
            reason="invite for new member (first external)",
        )
        print(f"INVITE: {inv.url}  (1 use, 24h, -> #{INVITE_CHANNEL})")
    except Exception as e:
        print(f"  ! invite failed: {e}")


async def action(guild):
    print(f"guild: {guild.name} ({guild.member_count} members)")
    print()

    # 1. charter
    ch_charter = discord.utils.get(guild.text_channels, name="the-charter")
    if ch_charter:
        posted = await post_pinned_embed(ch_charter, CHARTER_TITLE, CHARTER_BODY)
        print(f"charter: {'posted' if posted else 'already up'}")
    else:
        print("! #the-charter missing")

    # 2. thesis
    ch_thesis = discord.utils.get(guild.text_channels, name="the-thesis")
    if ch_thesis:
        posted = await post_pinned_embed(ch_thesis, THESIS_TITLE, THESIS_BODY)
        print(f"thesis: {'posted' if posted else 'already up'}")
    else:
        print("! #the-thesis missing")
    print()

    # 3. audit Void (pre-fix)
    print("--- PRE-FIX VOID AUDIT ---")
    await audit_void_perms(guild)
    print()

    # 4. enforce Void perms
    print("--- ENFORCING VOID PERMS ---")
    await fix_void_perms(guild)
    print()

    # 5. re-audit
    print("--- POST-FIX VOID AUDIT ---")
    await audit_void_perms(guild)
    print()

    # 6. owner roles
    print("--- OWNER ROLES ---")
    await assign_owner_roles(guild)
    print()

    # 7. invite
    print("--- INVITE ---")
    await make_invite(guild)


if __name__ == "__main__":
    run_admin(action)
