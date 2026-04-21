"""POST /think — force nexus to generate a single thought cycle now.

Skips the 45-90min cadence — fires `nexus_mind.think_now()` immediately and
posts the result to the #thoughts channel as an embed. Useful for debugging
the mind loop or smoke-testing persona/prompt changes.

Body (all optional):
    { "guild_id": int (optional — first guild if omitted) }

Response:
    { "ok": bool, "thought": "string | null", "guild_id": int | null }
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

import nexus_debug_http


log = logging.getLogger("nexus_think_api")


def _log(msg: str) -> None:
    line = f"[nexus_think_api] {msg}"
    print(line, flush=True)
    try:
        log.info(line)
    except Exception:
        pass


def _get_bot():
    return getattr(nexus_debug_http, "_bot_ref", None)


async def handle_think(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    bot = _get_bot()
    if bot is None or not bot.user:
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)

    # pick guild: explicit id or first guild
    guild_id = body.get("guild_id")
    if guild_id is not None:
        try:
            guild_id = int(guild_id)
        except Exception:
            return web.json_response({"ok": False, "error": "bad guild_id"}, status=400)
    else:
        if not bot.guilds:
            return web.json_response({"ok": False, "error": "bot in no guilds"}, status=503)
        guild_id = bot.guilds[0].id

    try:
        import nexus_mind
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"import nexus_mind failed: {type(e).__name__}: {e}"},
            status=500,
        )

    try:
        thought = await nexus_mind.think_now(bot, guild_id)
    except Exception as e:
        _log(f"think_now error: {type(e).__name__}: {e}")
        return web.json_response(
            {"ok": False, "error": f"think_now: {type(e).__name__}: {e}"},
            status=500,
        )

    out: dict[str, Any] = {
        "ok": bool(thought),
        "thought": thought,
        "guild_id": guild_id,
    }
    if not thought:
        out["note"] = "model returned no thought (SKIP, quiet window, or no channel)"
    _log(f"fired: {str(thought)[:80]!r}")
    return web.json_response(out, status=200)


def install(bot=None) -> None:
    """Register POST /think on the debug HTTP plane. Idempotent."""
    if getattr(install, "_installed", False):
        return
    install._installed = True  # type: ignore[attr-defined]
    try:
        nexus_debug_http.register_route("POST", "/think", handle_think)
        _log("think api installed (POST /think)")
    except Exception as e:
        _log(f"register_route failed: {type(e).__name__}: {e}")


__all__ = ["install", "handle_think"]
