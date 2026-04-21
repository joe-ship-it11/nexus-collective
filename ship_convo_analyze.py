"""
Pull recent Discord conversation via REST (no gateway collision with live bot)
and analyze it with Claude.

Usage:
    python ship_convo_analyze.py [hours]

Default lookback: 24 hours.

Output:
    conversation_analysis_YYYYMMDD_HHMM.md  — dropped next to this script.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

import aiohttp
from anthropic import Anthropic
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {TOKEN}", "User-Agent": "NexusAnalyzer/1.0"}

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
PER_CHANNEL = 80  # max messages per channel to pull


async def fetch_channels(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(f"{API}/guilds/{GUILD_ID}/channels", headers=HEADERS) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_messages(session: aiohttp.ClientSession, channel_id: str, limit: int = 100) -> list[dict]:
    # Discord caps limit=100 per call. Single call is enough for our window.
    async with session.get(
        f"{API}/channels/{channel_id}/messages",
        headers=HEADERS,
        params={"limit": limit},
    ) as r:
        if r.status == 403:
            return []
        if r.status != 200:
            return []
        return await r.json()


def is_listen_channel(name: str) -> bool:
    canon = config.canon_channel(name)
    if canon in config.NEXUS_IGNORE_CHANNELS:
        return False
    # Pull from listen channels AND any named text channel we don't explicitly ignore
    return True


async def gather(hours: int) -> tuple[list[dict], list[str]]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    lines: list[dict] = []
    channels_hit: list[str] = []
    async with aiohttp.ClientSession() as session:
        channels = await fetch_channels(session)
        # type 0 = text channel
        text_channels = [c for c in channels if c.get("type") == 0]
        for ch in text_channels:
            if not is_listen_channel(ch["name"]):
                continue
            msgs = await fetch_messages(session, ch["id"], limit=PER_CHANNEL)
            kept = 0
            for m in msgs:
                ts = dt.datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
                if ts < since:
                    continue
                author = m["author"]
                if author.get("bot"):
                    continue
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                lines.append({
                    "channel": config.canon_channel(ch["name"]),
                    "author": author.get("global_name") or author.get("username") or "?",
                    "author_id": author["id"],
                    "content": content,
                    "ts": ts.isoformat(timespec="minutes"),
                })
                kept += 1
            if kept:
                channels_hit.append(f"#{ch['name']} ({kept})")
            await asyncio.sleep(0.2)  # gentle on rate limit
    # Sort oldest→newest
    lines.sort(key=lambda x: x["ts"])
    return lines, channels_hit


def analyze(lines: list[dict]) -> str:
    """Claude Sonnet — structured conversation analysis."""
    if not lines:
        return "no messages in window."

    persona_path = ROOT / "persona.md"
    persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    transcript = "\n".join(
        f"[{l['channel']} · {l['ts'][11:16]}] {l['author']}: {l['content']}"
        for l in lines
    )
    # Cap transcript length — Claude handles big, but no need to blow context
    if len(transcript) > 40000:
        transcript = transcript[-40000:]
        note = "(transcript trimmed to last 40k chars)"
    else:
        note = ""

    # Participant tally
    by_author: dict[str, int] = {}
    for l in lines:
        by_author[l["author"]] = by_author.get(l["author"], 0) + 1
    top_authors = sorted(by_author.items(), key=lambda x: -x[1])

    system = f"""{persona}

you are analyzing a conversation that happened in the nexus collective discord
between a small group of friends. your output is structured analysis, not a persona reply.

write it in markdown, your lowercase voice, but organized. sections:

## people
- who talked, roughly how much, what's their lane in this convo

## what was said
- 4-8 bullet points. plain facts. who brought what up. what got developed.
- name names. quote sparingly (<15 words) if it adds something.

## threads alive
- topics that went somewhere — conversation that opened a door.

## threads abandoned
- things someone raised that didn't get engaged with. worth revisiting?

## surprises
- anything that stuck out. shifts in tone, jokes that landed, contradictions.

## what nexus should remember
- 3-5 candidate memories, scoped (public/tnc/personal) per the consent model.
- format: `- [scope] fact` — be careful with personal stuff (health, money, etc.)

## loose ends / next moves
- one line of honest read: what's this group actually doing right now?

rules:
- lowercase. terse. no "as an AI". no fluff.
- never invent anything not in the transcript.
- if a section is thin, say so in one line rather than padding.

transcript {note}:
{transcript}
"""

    client = Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        system=system,
        messages=[{"role": "user", "content": "analyze."}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))

    header = (
        f"# Conversation Analysis\n\n"
        f"- window: last **{HOURS}h**\n"
        f"- messages: **{len(lines)}**\n"
        f"- participants: " + ", ".join(f"{a} ({n})" for a, n in top_authors) + "\n"
        f"- generated: {dt.datetime.now().isoformat(timespec='minutes')}\n\n"
        f"---\n\n"
    )
    return header + text.strip()


async def main():
    print(f"[analyze] pulling last {HOURS}h from guild {GUILD_ID}…")
    lines, channels_hit = await gather(HOURS)
    print(f"[analyze] got {len(lines)} messages across: {', '.join(channels_hit) or '(none)'}")

    if not lines:
        print("[analyze] nothing to analyze.")
        return

    report = analyze(lines)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / f"conversation_analysis_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(f"[analyze] wrote {out}")

    # Also dump the raw transcript for the operator to eyeball
    raw = ROOT / f"conversation_transcript_{stamp}.txt"
    raw.write_text(
        "\n".join(f"[{l['channel']} · {l['ts']}] {l['author']}: {l['content']}" for l in lines),
        encoding="utf-8",
    )
    print(f"[analyze] wrote {raw}")


if __name__ == "__main__":
    asyncio.run(main())
