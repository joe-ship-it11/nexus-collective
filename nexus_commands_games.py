"""
/truthlie — the Nexus party game.

Pick a target user. Nexus pulls 2 real memories about them (public or
tnc-scoped — never personal), writes 1 plausible lie, shuffles the three,
and the room votes which is fake. Reveal after 45s.

Usage:
    import nexus_commands_games
    await nexus_commands_games.register(tree, DISCORD_GUILD_ID, bot)

register() is idempotent.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

import discord
from discord import app_commands

import config
import nexus_brain

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
VOTE_WINDOW_SECONDS = 45
MIN_REAL_STATEMENTS = 2
MAX_STATEMENT_CHARS = 180
LIE_MODEL = "claude-haiku-4-5-20251001"  # cheap + fast for one-shot fake
LIE_MAX_TOKENS = 200


# ---------------------------------------------------------------------------
# Memory → statement helpers
# ---------------------------------------------------------------------------
def _mem_text(mem: dict) -> str:
    return (mem.get("memory") or mem.get("text") or "").strip()


def _shareable_memories(user_id: str) -> list[dict]:
    """Return memories whose scope is 'public' or 'tnc' — safe to share in a game."""
    all_mems = nexus_brain.get_all_for_user(user_id, viewer_user_id=None)  # viewer=None hides personal
    out = []
    seen = set()
    for m in all_mems:
        text = _mem_text(m)
        if not text or len(text) < 12:
            continue
        if len(text) > MAX_STATEMENT_CHARS:
            text = text[:MAX_STATEMENT_CHARS - 1] + "…"
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "scope": nexus_brain._mem_scope(m)})
    return out


async def _fabricate_lie(target_name: str, real_statements: list[str]) -> Optional[str]:
    """Ask Claude Haiku for ONE plausible but false statement about target_name."""
    persona = nexus_brain._get_persona()
    real_block = "\n".join(f"- {s}" for s in real_statements)
    system = f"""{persona}

you are writing ONE false statement about {target_name} for a party game called truthlie.
the other statements in the round are REAL things you know about them (listed below).
your job: write a single plausible-sounding fake in the same tone and shape.

rules:
- output ONLY the fake statement. no preamble, no quotes, no "here's".
- 1 short sentence. third person. present tense if possible.
- must be plausible given the real ones — same domain or lifestyle, not wildly off.
- must actually be FALSE — invent something new, don't just restate a real one.
- no personal/sensitive topics (health, relationships, money, mental health).
- lowercase-ish, terse, honest-voice. no emojis.
- 40 to 160 chars.

real things you know about {target_name}:
{real_block}
"""
    client = nexus_brain._get_anthropic()
    try:
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=LIE_MODEL,
                max_tokens=LIE_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": f"one fake about {target_name}."}],
            )
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        # Strip wrapping quotes/bullets if Claude added any
        text = text.strip("-• ").strip().strip('"').strip("'").strip()
        if len(text) > MAX_STATEMENT_CHARS:
            text = text[:MAX_STATEMENT_CHARS - 1] + "…"
        return text or None
    except Exception as e:
        print(f"[truthlie] lie generation error: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Voting UI
# ---------------------------------------------------------------------------
class TruthlieView(discord.ui.View):
    def __init__(self, lie_index: int, target_display: str):
        super().__init__(timeout=VOTE_WINDOW_SECONDS)
        self.lie_index = lie_index  # 0, 1, or 2
        self.target_display = target_display
        self.votes: dict[int, int] = {}  # user_id → index they voted

        for i in range(3):
            btn = discord.ui.Button(
                label=str(i + 1),
                style=discord.ButtonStyle.secondary,
                custom_id=f"truthlie_{i}",
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, idx: int):
        async def cb(interaction: discord.Interaction):
            self.votes[interaction.user.id] = idx
            await interaction.response.send_message(
                f"locked in #{idx + 1}. we'll see.",
                ephemeral=True,
            )
        return cb

    def tally(self) -> dict[int, list[int]]:
        """Return {statement_idx: [user_ids who voted for it]}."""
        out = {0: [], 1: [], 2: []}
        for uid, idx in self.votes.items():
            out[idx].append(uid)
        return out


def _build_round_embed(target_display: str, statements: list[str], closes_in: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"truthlie — {target_display}",
        description=(
            f"two of these are real. one is a lie i made up.\n"
            f"vote with the buttons. reveal in **{closes_in}s**."
        ),
        color=0x3b82f6,
    )
    for i, s in enumerate(statements):
        embed.add_field(name=f"{i + 1}.", value=s, inline=False)
    embed.set_footer(text="nexus · truthlie")
    return embed


def _build_reveal_embed(
    target_display: str,
    statements: list[str],
    lie_idx: int,
    view: TruthlieView,
    bot: discord.Client,
) -> discord.Embed:
    tally = view.tally()
    total_votes = sum(len(v) for v in tally.values())
    embed = discord.Embed(
        title=f"truthlie — {target_display} — reveal",
        color=0x22c55e if total_votes else 0x6b7280,
    )
    for i, s in enumerate(statements):
        marker = "🚨 LIE" if i == lie_idx else "✓ real"
        voters = tally.get(i, [])
        voter_mentions = []
        for uid in voters[:10]:
            u = bot.get_user(uid)
            voter_mentions.append(u.mention if u else f"<@{uid}>")
        extra = f"\nvotes: {', '.join(voter_mentions)}" if voters else "\nvotes: (none)"
        embed.add_field(
            name=f"{i + 1}. — {marker}",
            value=f"{s}{extra}",
            inline=False,
        )
    winners = tally.get(lie_idx, [])
    if not total_votes:
        footer = "nobody voted. rude."
    elif winners:
        win_mentions = []
        for uid in winners[:10]:
            u = bot.get_user(uid)
            win_mentions.append(u.mention if u else f"<@{uid}>")
        footer = f"caught it: {', '.join(win_mentions)}"
    else:
        footer = "nobody caught the lie. i'm better at this than you thought."
    embed.set_footer(text=footer)
    return embed


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
async def register(tree: app_commands.CommandTree, guild_id: int, bot: discord.Client) -> None:
    if getattr(register, "_registered", False):
        return

    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="truthlie",
        description="two truths, one lie — nexus picks from its memory of you",
        guild=guild_obj,
    )
    @app_commands.describe(user="who to play on. omit to pick yourself.")
    async def truthlie(interaction: discord.Interaction, user: Optional[discord.Member] = None):
        await interaction.response.defer(thinking=True)
        target: discord.Member = user or interaction.user

        # Consent gate — never play with someone who opted out of memory
        try:
            import nexus_consent
            if nexus_consent.is_opted_out(str(target.id)):
                await interaction.followup.send(
                    f"{target.display_name} opted out of memory. can't play with them.",
                    ephemeral=True,
                )
                return
        except Exception:
            pass

        # Pull shareable memories
        shareable = await asyncio.to_thread(_shareable_memories, str(target.id))
        if len(shareable) < MIN_REAL_STATEMENTS:
            if target.id == interaction.user.id:
                msg = (
                    f"memory's too thin on you for truthlie. "
                    f"talk more — public/tnc stuff — then come back."
                )
            else:
                msg = (
                    f"memory's too thin on {target.display_name} for truthlie. "
                    f"need at least {MIN_REAL_STATEMENTS} public/tnc memories."
                )
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Pick 2 real statements at random
        picks = random.sample(shareable, 2)
        real_texts = [p["text"] for p in picks]

        # Fabricate 1 lie
        lie = await _fabricate_lie(target.display_name, real_texts)
        if not lie:
            await interaction.followup.send(
                "brain fart. couldn't generate a lie. try again.",
                ephemeral=True,
            )
            return

        # Shuffle
        statements = real_texts + [lie]
        order = list(range(3))
        random.shuffle(order)
        shuffled = [statements[order[i]] for i in range(3)]
        lie_position = order.index(2)  # new index of the lie (original index 2)

        # Post round
        view = TruthlieView(lie_index=lie_position, target_display=target.display_name)
        round_embed = _build_round_embed(
            target.display_name,
            shuffled,
            closes_in=VOTE_WINDOW_SECONDS,
        )
        msg = await interaction.followup.send(embed=round_embed, view=view)

        # Wait for vote window, then reveal
        async def _reveal_later():
            await asyncio.sleep(VOTE_WINDOW_SECONDS)
            for child in view.children:
                if hasattr(child, "disabled"):
                    child.disabled = True
            view.stop()
            reveal = _build_reveal_embed(
                target.display_name,
                shuffled,
                lie_position,
                view,
                bot,
            )
            try:
                await msg.edit(embed=reveal, view=view)
            except Exception as e:
                print(f"[truthlie] reveal edit error: {type(e).__name__}: {e}")

        asyncio.create_task(_reveal_later())

    register._registered = True
