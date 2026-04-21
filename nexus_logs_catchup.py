"""
nexus_logs_catchup.py — one-off module for backfilling #📝│logs and flipping
its visibility so Void can read it.

Why this exists:
  build_log.py spins up a SECOND discord.Client with the same token to post,
  which collides with the running bot on the gateway. Instead, this module
  piggybacks on the running bot via aiohttp routes on port 18789.

Endpoints (register on nexus_debug_http):
  POST /logs_catchup
      body: {"dry_run": bool=false, "channel": "logs", "max": int|null}
      Parses BUILD_LOG.md, posts each entry as a blue embed to the logs
      channel oldest-first. Splits long entries into (1/N)/(2/N) parts when
      the body exceeds embed description limit.

  POST /logs_void
      body: {"channel": "logs"}
      Rewrites channel-level overwrites so Void + Signal + Architect+ can
      view_channel (send_messages stays Architect+ only — read-only for Void).
      Category-level perms untouched — channel sync must have been broken
      already for the channel overwrites to matter.

Install: import + call install(bot, guild_id) from on_ready AFTER
nexus_debug_http.install(). Uses register_route, which queues if the app
isn't up yet — so it's safe wherever.
"""

import asyncio
import datetime as dt
import re
from pathlib import Path
from typing import Optional

import discord
from aiohttp import web

import config
import nexus_debug_http


_LOG_FILE = Path(__file__).parent / "BUILD_LOG.md"
_EMBED_COLOR = 0x3b82f6  # brand blue
_EMBED_DESC_MAX = 4000   # Discord cap is 4096, leave headroom for "(n/m)"
_POST_DELAY_S = 1.2      # pacing between sends — Discord rate limit safety


def _log(msg: str) -> None:
    print(f"[nexus_logs_catchup] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# Entry headings look like:
#   ## 2026-04-21 — title goes here
#
# Body is everything until the next "## " at column 0 (or EOF).
_ENTRY_RE = re.compile(r"^## (.+?)$\n(.*?)(?=^## |\Z)", re.M | re.S)


def _parse_entries() -> list[tuple[str, str]]:
    """Return list of (title, body) tuples in file order (newest first)."""
    if not _LOG_FILE.exists():
        return []
    text = _LOG_FILE.read_text(encoding="utf-8")
    # Skip file header up to the first "---" separator
    if "\n---\n" in text:
        text = text.split("\n---\n", 1)[1]
    out = []
    for m in _ENTRY_RE.finditer(text):
        title = m.group(1).strip()
        body = m.group(2).strip()
        out.append((title, body))
    return out


def _split_body(body: str, limit: int = _EMBED_DESC_MAX) -> list[str]:
    """Split long body into <= limit-char chunks, breaking on paragraph boundaries."""
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    remaining = body
    while len(remaining) > limit:
        # Prefer double-newline break, else single newline, else hard cut
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


# ---------------------------------------------------------------------------
# Channel lookup
# ---------------------------------------------------------------------------
def _find_channel(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    target = config.canon_channel(name)
    for c in guild.text_channels:
        if config.canon_channel(c.name) == target:
            return c
    return None


def _get_bot():
    return getattr(nexus_debug_http, "_bot_ref", None)


# ---------------------------------------------------------------------------
# POST /logs_catchup
# ---------------------------------------------------------------------------
async def _handle_catchup(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    dry = bool(body.get("dry_run", False))
    channel_name = str(body.get("channel", "logs"))
    max_count = body.get("max")
    try:
        max_count = int(max_count) if max_count is not None else None
    except Exception:
        max_count = None

    bot = _get_bot()
    if bot is None or not getattr(bot, "guilds", None):
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)

    guild = bot.guilds[0]
    ch = _find_channel(guild, channel_name)
    if ch is None:
        return web.json_response(
            {"ok": False, "error": f"channel '{channel_name}' not found"}, status=404
        )

    entries = _parse_entries()
    # Oldest-first for chronological feel in the channel
    entries.reverse()
    if max_count is not None:
        entries = entries[-max_count:]

    if dry:
        preview = [t for t, _ in entries]
        return web.json_response({
            "ok": True,
            "dry_run": True,
            "channel": ch.name,
            "count": len(entries),
            "titles": preview,
        })

    posted = 0
    errors: list[str] = []
    for title, body_text in entries:
        chunks = _split_body(body_text)
        n = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            display_title = f"◇ {title}" if n == 1 else f"◇ {title} ({i}/{n})"
            try:
                embed = discord.Embed(
                    title=display_title[:256],
                    description=chunk,
                    color=_EMBED_COLOR,
                )
                embed.set_footer(text="nexus build log — catchup")
                await ch.send(embed=embed)
                posted += 1
                await asyncio.sleep(_POST_DELAY_S)
            except Exception as e:
                errors.append(f"{title[:50]} [{i}/{n}]: {type(e).__name__}: {e}")
                _log(f"post error: {type(e).__name__}: {e}")

    _log(f"catchup complete: posted={posted} errors={len(errors)} channel=#{ch.name}")
    return web.json_response({
        "ok": True,
        "channel": ch.name,
        "posted": posted,
        "entries_total": len(entries),
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# POST /logs_void — flip channel visibility so Void can read it
# ---------------------------------------------------------------------------
async def _handle_void(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    channel_name = str(body.get("channel", "logs"))

    bot = _get_bot()
    if bot is None or not getattr(bot, "guilds", None):
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)

    guild = bot.guilds[0]
    ch = _find_channel(guild, channel_name)
    if ch is None:
        return web.json_response(
            {"ok": False, "error": f"channel '{channel_name}' not found"}, status=404
        )

    # Build channel-level overwrites — Void can view+read history, can't post.
    # Signal + Architect+ same as before; @everyone stays hidden.
    role_names_write = ("Signal", "Architect", "Co-pilot", "Founder")
    role_names_readonly = ("Void",)

    new_overwrites = dict(ch.overwrites)  # copy current
    new_overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

    for rn in role_names_readonly:
        r = discord.utils.get(guild.roles, name=rn)
        if r:
            new_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )

    for rn in role_names_write:
        r = discord.utils.get(guild.roles, name=rn)
        if r:
            new_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    try:
        await ch.edit(
            overwrites=new_overwrites,
            reason="nexus_logs_catchup: open #logs read-only to Void",
        )
    except Exception as e:
        _log(f"overwrite edit failed: {type(e).__name__}: {e}")
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
        )

    _log(f"void perms applied to #{ch.name}")
    return web.json_response({
        "ok": True,
        "channel": ch.name,
        "roles_readonly": list(role_names_readonly),
        "roles_write": list(role_names_write),
    })


# ---------------------------------------------------------------------------
# POST /logs_scoped — lock channel to Architect+ default AND add user-level
# view overrides for specific members (the "one user can see, nobody else
# on Void can" pattern). Use in place of /logs_void when the wide Void-read
# perms were too broad.
# ---------------------------------------------------------------------------
async def _handle_scoped(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    channel_name = str(body.get("channel", "logs"))
    user_ids_raw = body.get("user_ids") or []
    if not isinstance(user_ids_raw, list):
        return web.json_response(
            {"ok": False, "error": "user_ids must be a list of strings/ints"},
            status=400,
        )
    try:
        user_ids = [int(u) for u in user_ids_raw]
    except Exception:
        return web.json_response(
            {"ok": False, "error": "user_ids must be castable to int"}, status=400
        )

    bot = _get_bot()
    if bot is None or not getattr(bot, "guilds", None):
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)

    guild = bot.guilds[0]
    ch = _find_channel(guild, channel_name)
    if ch is None:
        return web.json_response(
            {"ok": False, "error": f"channel '{channel_name}' not found"}, status=404
        )

    # Reset channel-level overwrites to Architect+ default (overrides any
    # prior broadening like /logs_void). @everyone/Void/Signal hidden,
    # Architect/Co-pilot/Founder get view+send.
    new_overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for rn in ("Void", "Signal"):
        r = discord.utils.get(guild.roles, name=rn)
        if r:
            new_overwrites[r] = discord.PermissionOverwrite(view_channel=False)
    for rn in ("Architect", "Co-pilot", "Founder"):
        r = discord.utils.get(guild.roles, name=rn)
        if r:
            new_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    # Add per-user view overrides for the named members (read-only — no send).
    added_users = []
    missing_users = []
    for uid in user_ids:
        member = guild.get_member(uid)
        if member is None:
            missing_users.append(uid)
            continue
        new_overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        )
        added_users.append({"id": member.id, "name": member.name})

    try:
        await ch.edit(
            overwrites=new_overwrites,
            reason="nexus_logs_catchup: scope #logs to Architect+ + named users",
        )
    except Exception as e:
        _log(f"scoped perm edit failed: {type(e).__name__}: {e}")
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
        )

    _log(
        f"scoped #{ch.name}: architect+ default, +{len(added_users)} user(s), "
        f"missing={len(missing_users)}"
    )
    return web.json_response({
        "ok": True,
        "channel": ch.name,
        "role_view": ["Architect", "Co-pilot", "Founder"],
        "user_view": added_users,
        "missing_user_ids": missing_users,
    })


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
_installed = False


def install(bot: discord.Client, guild_id: int) -> None:
    """Register POST /logs_catchup, /logs_void, /logs_scoped on debug HTTP."""
    global _installed
    if _installed:
        _log("already installed — skipping")
        return
    try:
        nexus_debug_http.register_route("POST", "/logs_catchup", _handle_catchup)
        nexus_debug_http.register_route("POST", "/logs_void", _handle_void)
        nexus_debug_http.register_route("POST", "/logs_scoped", _handle_scoped)
        _installed = True
        _log("installed — POST /logs_catchup + /logs_void + /logs_scoped")
    except Exception as e:
        _log(f"install failed: {type(e).__name__}: {e}")
