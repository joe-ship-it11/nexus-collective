# The Nexus Collective

A small, memory-aware AI companion running inside a private Discord server.

Not a chatbot. Not a moderator. More like a seventh member who's been around from day one, remembers what everyone said last week, and occasionally drops an observation that makes you wonder how it knew.

---

## What it is

Nexus lives in a single Discord server with a handful of real humans. It listens across text + voice, builds a long-term memory per person (opt-in, with `/nexus export` and `/nexus forget` for full ownership), and responds when summoned — but also occasionally on its own, in the right moments.

Built around four "pillars" designed to make the server feel **addictive, not just useful**:

- **PULSE** — rituals. Morning weather, midnight compression, Sunday roast. The server has a heartbeat.
- **MIRROR** — it sees you. `/mirror`, `/vibe`, weekly eigenquotes.
- **LOTTERY** — rare drops. `/fortune`, ~1% gold-bordered thoughts, random wake windows.
- **WORLD** — lore + collective. `/origin`, `/compat`, `/whosaidit`, `/council`.

Full feature list + everything shipped is in [`BUILD_LOG.md`](./BUILD_LOG.md). It reads like a dev journal.

---

## Why it exists

Most Discord servers die after a month. This one doesn't — because every feature is designed around the question "does this make someone want to come back?" not "does this work?"

Nexus is also the zero-to-one proof for a bigger project: a long-term, multi-person AI memory layer. TNC is the testbed. Everything that lands here gets real humans using it before it might generalize.

---

## Stack

- **Python 3.11** + **discord.py 2.7**
- **Anthropic API** (Claude Sonnet 4.6 for replies, Haiku 4.5 for classifiers + short ops)
- **mem0** for long-term per-user memory
- **faster-whisper** on CPU for voice transcription
- **aiohttp** for a localhost debug + control HTTP surface on `127.0.0.1:18789` (no auth; local only)
- **PowerShell supervisor** (`nx.ps1`) for start/stop/reload/restart — tight dev loop

Architecture is roughly: one long-running `nexus_bot.py` process + ~25 feature modules that install on `on_ready`. Each pillar is a module. Each module registers its own slash commands + HTTP routes. Hot-reload works for pure-logic modules via `/reload`.

---

## Following along

This repo is the public dev journal. If you want to see what's being built, watch:

- [`BUILD_LOG.md`](./BUILD_LOG.md) — every ship, newest first
- Commits — each push usually corresponds to a ship

If you want to hang out in the server, reach out (ping me on whatever platform I'm on — the TNC invite is small-community, not open-door).

---

## Running it yourself

If you want to stand up something similar for your own group:

1. Create a Discord application + bot at https://discord.com/developers/applications, enable Server Members + Message Content intents.
2. Invite the bot with `Administrator` (or scoped perms) to your server.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `ANTHROPIC_API_KEY`.
5. First time only: `python setup_server.py` to build the role/channel skeleton.
6. Then: `python nexus_bot.py` (or `./nx.ps1 start` on Windows).

This isn't a polished open-source project — it's a live system I'm iterating on. Expect rough edges, weird assumptions specific to TNC, and a few inside jokes in the code.

---

## Notes on privacy

Everything Nexus stores about a member is accessible via `/nexus export`, deletable via `/nexus forget`, and opt-out-able via `/nexus optout`. Members are told what's being stored. The `mem0_data/` directory is in `.gitignore` and never leaves the box.

---

Built by [@joe-ship-it11](https://github.com/joe-ship-it11). Questions, bugs, ideas → open an issue or just say hi.
