"""POST /say — make nexus speak from PowerShell without touching Discord UI.

Lets the dev (or Claude) trigger nexus speech as either:
  * a TTS line in whatever voice channel nexus is currently in
  * a text message posted to a named text channel
  * both, with one call

Body schema:
    {
        "text":    "string (required) — what to say",
        "channel": "string (optional) — text channel name (e.g. 'chat')",
        "voice":   true | false (optional) — if true, speak via TTS in current VC,
        "voice_text": "string (optional) — different text for voice vs. text channel"
    }

At least one of `channel` or `voice` (true) must be present, else 400.

Response:
    { "ok": bool, "voice": {...} | null, "text": {...} | null, "errors": {...} }

Endpoint registered on the existing nexus_debug_http aiohttp app at install time.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

import nexus_debug_http


log = logging.getLogger("nexus_say_api")


def _log(msg: str) -> None:
    line = f"[nexus_say_api] {msg}"
    print(line, flush=True)
    try:
        log.info(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_bot():
    return getattr(nexus_debug_http, "_bot_ref", None)


def _find_text_channel(bot, name: str):
    """Find a text channel by name across the bot's guilds, tolerant of emoji prefixes."""
    try:
        import config  # local import to dodge import-cycle surprises
    except Exception:
        config = None  # type: ignore[assignment]
    name_low = name.lower()
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name.lower() == name_low:
                return ch
            try:
                if config and config.canon_channel(ch.name) == name_low:
                    return ch
            except Exception:
                pass
    return None


def _first_voice_client(bot):
    """First connected voice client across all guilds. Most setups only have one."""
    for guild in bot.guilds:
        vc = guild.voice_client
        if vc and vc.is_connected():
            return vc
    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
async def handle_say(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"bad json: {type(e).__name__}: {e}"},
            status=400,
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": "body must be a JSON object"},
            status=400,
        )

    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response(
            {"ok": False, "error": "missing 'text'"},
            status=400,
        )

    channel_name = body.get("channel")
    want_voice = bool(body.get("voice"))
    voice_text = str(body.get("voice_text") or text).strip() or text

    if not channel_name and not want_voice:
        return web.json_response(
            {"ok": False, "error": "specify 'channel' (text) and/or 'voice':true"},
            status=400,
        )

    bot = _get_bot()
    if bot is None or not bot.user:
        return web.json_response(
            {"ok": False, "error": "bot not ready"},
            status=503,
        )

    out: dict[str, Any] = {"ok": True, "voice": None, "text": None, "errors": {}}

    # --- text channel post ---
    if channel_name:
        ch = _find_text_channel(bot, str(channel_name))
        if ch is None:
            out["errors"]["text"] = f"channel not found: {channel_name!r}"
            out["ok"] = False
        else:
            try:
                msg = await ch.send(text[:1950])
                out["text"] = {
                    "channel": ch.name,
                    "channel_id": ch.id,
                    "message_id": msg.id,
                    "len": len(text),
                }
                _log(f"posted to #{ch.name}: {text[:80]!r}")
            except Exception as e:
                out["errors"]["text"] = f"send failed: {type(e).__name__}: {e}"
                out["ok"] = False

    # --- voice TTS ---
    if want_voice:
        try:
            import discord
            import nexus_voice
        except Exception as e:
            out["errors"]["voice"] = f"import failed: {type(e).__name__}: {e}"
            out["ok"] = False
        else:
            vc = _first_voice_client(bot)
            if vc is None:
                out["errors"]["voice"] = "not connected to any voice channel — /join first"
                out["ok"] = False
            else:
                try:
                    path = await nexus_voice.synthesize(voice_text)
                    if vc.is_playing():
                        vc.stop()
                    source = discord.FFmpegPCMAudio(str(path))
                    vc.play(source, after=nexus_voice.cleanup_callback(path))
                    out["voice"] = {
                        "channel": vc.channel.name if vc.channel else None,
                        "channel_id": vc.channel.id if vc.channel else None,
                        "len": len(voice_text),
                    }
                    _log(f"speaking in #{vc.channel.name if vc.channel else '?'}: {voice_text[:80]!r}")
                except FileNotFoundError as e:
                    out["errors"]["voice"] = f"ffmpeg missing: {e}"
                    out["ok"] = False
                except Exception as e:
                    out["errors"]["voice"] = f"play failed: {type(e).__name__}: {e}"
                    out["ok"] = False

    status = 200 if out["ok"] else 207  # 207 = multi-status (partial success)
    return web.json_response(out, status=status)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
def install(bot=None) -> None:
    """Register POST /say on the debug HTTP plane. Idempotent."""
    if getattr(install, "_installed", False):
        return
    install._installed = True  # type: ignore[attr-defined]
    try:
        nexus_debug_http.register_route("POST", "/say", handle_say)
        _log("say api installed (POST /say)")
    except Exception as e:
        _log(f"register_route failed: {type(e).__name__}: {e}")


__all__ = ["install", "handle_say"]
