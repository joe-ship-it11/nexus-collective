"""
Local HTTP debug surface for TNC Nexus.

Purpose: let Claude (or the user) read bot state in one shot without
tailing logs, surfacing Discord, or screenshotting anything.

Binds to 127.0.0.1:18789 only. No auth. Not exposed off-box.

Endpoints:
    GET  /ping                  — lightweight "is the bot alive + ready" check
    GET  /state                 — one JSON blob with everything that matters
    GET  /tail?lines=N          — raw last N lines of nexus_bot.log
    GET  /logs?tail=N           — alias for /tail
    GET  /transcripts?limit=N   — last N voice transcript entries (JSONL)
    POST /probe   {"text": "..."}   — would this text trigger nexus?
    POST /reload  {"module": "…"}  — hot-reload a pure-logic module (allow-listed)
    POST /restart               — spawn fresh detached bot + os._exit(0)
    POST /kill                  — graceful shutdown, no respawn
    GET  /                      — index listing endpoints

Usage from PowerShell:
    curl http://127.0.0.1:18789/state | ConvertFrom-Json

Usage from Python:
    import json, urllib.request
    print(json.load(urllib.request.urlopen("http://127.0.0.1:18789/state")))
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from aiohttp import web

import config

# ---------------------------------------------------------------------------
# State — ring buffers written by nexus_bot.py
# ---------------------------------------------------------------------------
_RECENT_TEXT: deque = deque(maxlen=80)
_RECENT_ERRORS: deque = deque(maxlen=40)
_bot_ref = None

# The aiohttp app is created in install() and stashed here so external
# modules (nexus_eyes) can attach extra routes via register_route().
# Route additions must happen BEFORE the runner finishes setup — in practice
# that means in the same synchronous on_ready block, before the event loop
# yields to the _runner() task spawned in install().
_app = None  # type: ignore[assignment]
_pending_routes: list = []  # (method, path, handler) queued before install()

LOG_FILE = Path(__file__).parent / "nexus_bot.log"
TRANSCRIPT_LOG = Path(__file__).parent / "voice_transcripts.jsonl"

_TRIGGER_RE = re.compile(r"\bnexus\b", re.IGNORECASE)


def record_message(
    channel: str,
    author: str,
    author_id: int,
    content: str,
    triggered: bool,
    reason: str,
) -> None:
    """Called from nexus_bot.on_message — every inbound text message."""
    _RECENT_TEXT.append({
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "channel": channel,
        "author": author,
        "author_id": str(author_id),
        "content": (content or "")[:500],
        "triggered": bool(triggered),
        "reason": reason,
    })


def record_error(msg: str) -> None:
    _RECENT_ERRORS.append({
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "msg": str(msg)[:800],
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tail_transcripts(n: int = 30) -> list:
    if not TRANSCRIPT_LOG.exists():
        return []
    try:
        lines = TRANSCRIPT_LOG.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                out.append({"raw": ln})
        return out
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]


def _tail_log(n: int) -> str:
    if not LOG_FILE.exists():
        return ""
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-n:]
        return "\n".join(lines)
    except Exception as e:
        return f"[tail error: {type(e).__name__}: {e}]"


def _voice_snapshot(guild) -> dict:
    vc = guild.voice_client if guild else None
    if not vc or not vc.is_connected():
        return {"connected": False}
    snap = {
        "connected": True,
        "channel": vc.channel.name if vc.channel else None,
        "channel_id": vc.channel.id if vc.channel else None,
        "members": [m.display_name for m in (vc.channel.members if vc.channel else []) if not m.bot],
        "listening": bool(getattr(vc, "is_listening", lambda: False)()),
        "playing": bool(getattr(vc, "is_playing", lambda: False)()),
    }
    # DAVE session introspection
    try:
        conn = getattr(vc, "_connection", None)
        ds = getattr(conn, "dave_session", None) if conn else None
        if ds is not None:
            snap["dave"] = {
                "present": True,
                "ready": bool(getattr(ds, "ready", False)),
                "protocol_version": getattr(ds, "protocol_version", None),
                "status": str(getattr(ds, "status", "?")),
            }
        else:
            snap["dave"] = {"present": False}
    except Exception as e:
        snap["dave"] = {"error": f"{type(e).__name__}: {e}"}
    return snap


# ---------------------------------------------------------------------------
# Reload allow-list — only pure-logic modules are safe to hot-reload.
# Anything that registers discord event handlers / slash commands needs a
# full process restart to re-bind — those go through /restart instead.
# ---------------------------------------------------------------------------
_RELOADABLE = {
    "nexus_brain",
    "nexus_call_summary",
    "nexus_video",
    "nexus_mind",
    "nexus_consent",
    "nexus_profiles",
    "config",
}


# Post-reload hooks: callbacks that run after a successful importlib.reload
# of a given module. Lets patcher modules (e.g. nexus_lottery, which wraps
# nexus_mind._post_thought) reinstate their monkey-patches automatically so
# that `/reload nexus_mind` doesn't silently blow them away.
#
# Shape: {module_name: [callable, ...]}. Register via register_post_reload_hook.
_POST_RELOAD_HOOKS: "dict[str, list]" = {}


def register_post_reload_hook(module_name: str, callback) -> None:
    """Register a callback to fire after `/reload <module_name>` succeeds.

    Typical use: a module that monkey-patches `module_name` registers its
    re-patch function here so the patch survives an external hot-reload.
    Idempotent per (module, callback) pair.
    """
    _POST_RELOAD_HOOKS.setdefault(module_name, [])
    if callback not in _POST_RELOAD_HOOKS[module_name]:
        _POST_RELOAD_HOOKS[module_name].append(callback)


def _spawn_detached_restart(log_path: Path) -> int:
    """Spawn a fresh nexus_bot.py detached from this process, append-log.

    Returns the pid of the cmd.exe launcher wrapper (not the python child
    — cmd spawns python and exits, python outlives both).

    The tricky bits, learned the hard way:
    1. Can't `open(log_path, "ab")` from the parent — the parent's own
       cmd.exe wrapper already has a share-exclusive handle on the log,
       Windows raises PermissionError [Errno 13].
    2. Can't use `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on cmd.exe
       — the nested python child died silently (cmd appears to need a
       console to parse the `>>` redirect operator reliably).
    3. `CREATE_NO_WINDOW` works: cmd runs hidden, parses the redirect,
       spawns python, exits cleanly. Python inherits nothing that dies
       with the parent, so it outlives the parent's os._exit. Mirrors
       the `nx start` pattern byte-for-byte.
    """
    here = Path(__file__).parent.resolve()
    if os.name == "nt":
        # Use PowerShell's Start-Process -WindowStyle Hidden — same pattern
        # as `nx start`. Plus a `timeout 2` prefix: when /restart is fired
        # mid-session, the PARENT's own cmd.exe wrapper still has
        # nexus_bot.log open. The child's `>> log` redirect would collide
        # and exit silently. Waiting 2s gives the parent time to die
        # (scheduled exit fires ~1s after response) so the log handle is
        # free. `> nul` on timeout suppresses its own output.
        ps_cmd = (
            "Start-Process -FilePath cmd.exe "
            "-ArgumentList '/c',"
            f"'timeout /t 2 /nobreak > nul && "
            f"python nexus_bot.py >> \"{str(log_path)}\" 2>&1' "
            f"-WorkingDirectory '{str(here)}' -WindowStyle Hidden"
        )
        p = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            cwd=str(here),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        # POSIX path — open + pass is fine; no exclusive-handle problem.
        log_f = open(log_path, "ab")
        p = subprocess.Popen(
            [sys.executable, "nexus_bot.py"],
            cwd=str(here),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    return p.pid


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _index(_req):
    return web.json_response({
        "service": "nexus_debug_http",
        "endpoints": [
            "GET  /ping",
            "GET  /state",
            "GET  /tail?lines=100",
            "GET  /logs?tail=200        (alias for /tail)",
            "GET  /transcripts?limit=30",
            "POST /probe    body: {\"text\": \"...\"}",
            "POST /reload   body: {\"module\": \"nexus_brain\"}",
            "POST /restart  — spawn fresh bot, exit self",
            "POST /kill     — graceful shutdown, no respawn",
        ],
        "reloadable_modules": sorted(_RELOADABLE),
    })


async def _ping(_req):
    bot = _bot_ref
    ready = bool(bot and bot.user)
    return web.json_response({
        "ok": True,
        "ready": ready,
        "pid": os.getpid(),
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    })


async def _reload(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    mod_name = str(body.get("module", "")).strip()
    if not mod_name:
        return web.json_response(
            {"ok": False, "reason": "missing 'module' in body"}, status=400,
        )
    if mod_name not in _RELOADABLE:
        return web.json_response({
            "ok": False,
            "reason": f"'{mod_name}' is not in the reload allow-list",
            "reloadable": sorted(_RELOADABLE),
        }, status=400)
    try:
        import sys as _sys
        if mod_name not in _sys.modules:
            mod = importlib.import_module(mod_name)
        else:
            mod = _sys.modules[mod_name]
        importlib.reload(mod)

        # Fire any post-reload hooks — e.g. re-apply monkey-patches that a
        # reload of this module would have blown away. Hooks are sync-or-async,
        # isolated via try/except so one failing hook doesn't abort the rest.
        hooks_fired = []
        hooks_errors = []
        for cb in list(_POST_RELOAD_HOOKS.get(mod_name, [])):
            try:
                result = cb()
                if asyncio.iscoroutine(result):
                    await result
                hooks_fired.append(getattr(cb, "__name__", repr(cb)))
            except Exception as he:
                hooks_errors.append(
                    f"{getattr(cb, '__name__', repr(cb))}: "
                    f"{type(he).__name__}: {he}"
                )
                print(
                    f"[nexus_debug_http] post-reload hook error on "
                    f"{mod_name}: {type(he).__name__}: {he}",
                    flush=True,
                )

        return web.json_response({
            "ok": True,
            "module": mod_name,
            "hooks_fired": hooks_fired,
            "hooks_errors": hooks_errors,
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        return web.json_response({
            "ok": False,
            "module": mod_name,
            "reason": f"{type(e).__name__}: {e}",
        }, status=500)


async def _restart(_req):
    """Spawn fresh detached bot, then exit this process after reply is flushed.

    Exit is scheduled as an asyncio task (NOT a daemon thread + os._exit)
    so aiohttp has time to serialize + write the response to the socket
    before the loop dies. Previously a 0.6s daemon-thread sleep + os._exit
    raced the response write and clients saw `null` bodies.
    """
    log_path = Path(__file__).parent / "nexus_bot.log"
    try:
        child_pid = _spawn_detached_restart(log_path)
    except Exception as e:
        print(
            f"[nexus_debug_http] /restart spawn failed: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        return web.json_response(
            {"ok": False, "reason": f"{type(e).__name__}: {e}"},
            status=500,
        )

    async def _scheduled_exit():
        # Give aiohttp the event loop for ~1s to flush the response.
        await asyncio.sleep(1.0)
        print(
            f"[nexus_debug_http] /restart scheduled exit firing — "
            f"child_pid={child_pid}",
            flush=True,
        )
        os._exit(0)

    asyncio.create_task(_scheduled_exit())
    return web.json_response({
        "ok": True,
        "action": "restart",
        "child_pid": child_pid,
        "old_pid": os.getpid(),
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    })


async def _kill(_req):
    """Graceful shutdown with NO respawn. Supervisor must bring it back."""
    async def _scheduled_exit():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_scheduled_exit())
    return web.json_response({
        "ok": True,
        "action": "kill",
        "pid": os.getpid(),
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    })


async def _state(_req):
    out = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "bot": {"ready": False},
        "voice": {"connected": False},
        "recent_text": list(_RECENT_TEXT),
        "recent_errors": list(_RECENT_ERRORS),
        "recent_voice": _tail_transcripts(30),
    }
    bot = _bot_ref
    if bot and bot.user:
        out["bot"] = {
            "ready": True,
            "user": str(bot.user),
            "id": bot.user.id,
            "guilds": [{"id": g.id, "name": g.name, "members": g.member_count} for g in bot.guilds],
        }
        # Primary guild voice state
        guild = next(iter(bot.guilds), None)
        out["voice"] = _voice_snapshot(guild)
    # counts
    out["counts"] = {
        "recent_text": len(_RECENT_TEXT),
        "recent_errors": len(_RECENT_ERRORS),
        "recent_voice": len(out["recent_voice"]),
        "triggered": sum(1 for m in _RECENT_TEXT if m.get("triggered")),
    }
    return web.json_response(out)


async def _tail(req):
    try:
        n = int(req.query.get("lines", "100"))
    except ValueError:
        n = 100
    n = max(1, min(n, 5000))
    return web.Response(text=_tail_log(n), content_type="text/plain")


async def _transcripts(req):
    try:
        n = int(req.query.get("limit", "30"))
    except ValueError:
        n = 30
    n = max(1, min(n, 1000))
    return web.json_response(_tail_transcripts(n))


async def _probe(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = str(body.get("text", ""))
    channel = str(body.get("channel", ""))
    addressed_by_name = bool(_TRIGGER_RE.search(text))
    is_ignored = channel in getattr(config, "NEXUS_IGNORE_CHANNELS", set())
    reason = []
    if is_ignored:
        reason.append(f"channel '{channel}' is in NEXUS_IGNORE_CHANNELS — only @-mention triggers")
    if addressed_by_name and not is_ignored:
        reason.append("matched \\bnexus\\b")
    if not addressed_by_name and not is_ignored:
        reason.append("no 'nexus' name-trigger and no @-mention checked here (simulate @-mention by setting channel=... doesn't simulate mentions)")
    return web.json_response({
        "text": text,
        "channel": channel,
        "name_trigger_hit": addressed_by_name,
        "channel_ignored": is_ignored,
        "would_reply_if_mentioned": True,
        "would_reply_on_name_alone": addressed_by_name and not is_ignored,
        "reason": " | ".join(reason) if reason else "clean",
    })


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
def register_route(method: str, path: str, handler) -> None:
    """Attach an extra aiohttp route to the debug HTTP surface.

    Safe to call before or after install(). If called before install(), the
    route is queued and drained during install(). If called after install()
    but before the runner finishes freezing the app, it's attached directly.
    Calls after the app has frozen will be ignored with a log line — freeze
    happens on the first event-loop tick after install() returns, so register
    from on_ready synchronously after nexus_debug_http.install().
    """
    method = (method or "GET").upper()
    if _app is None:
        _pending_routes.append((method, path, handler))
        return
    try:
        _app.router.add_route(method, path, handler)
    except Exception as e:
        print(
            f"[nexus_debug_http] register_route({method} {path}) failed: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


def install(bot, port: int = 18789) -> None:
    """
    Spin up the HTTP server as a background task. Call from on_ready.
    Safe to call multiple times — no-ops after first start.
    """
    global _bot_ref, _app
    _bot_ref = bot
    if getattr(install, "_started", False):
        return
    install._started = True

    app = web.Application()
    _app = app
    app.router.add_get("/", _index)
    app.router.add_get("/ping", _ping)
    app.router.add_get("/state", _state)
    app.router.add_get("/tail", _tail)
    app.router.add_get("/logs", _tail)          # alias
    app.router.add_get("/transcripts", _transcripts)
    app.router.add_post("/probe", _probe)
    app.router.add_post("/reload", _reload)
    app.router.add_post("/restart", _restart)
    app.router.add_post("/kill", _kill)

    # Drain any routes queued before install() ran
    while _pending_routes:
        meth, pth, hndl = _pending_routes.pop(0)
        try:
            app.router.add_route(meth, pth, hndl)
        except Exception as e:
            print(
                f"[nexus_debug_http] pending register_route({meth} {pth}) "
                f"failed: {type(e).__name__}: {e}",
                flush=True,
            )

    async def _runner():
        try:
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            print(f"[nexus_debug_http] listening on http://127.0.0.1:{port}", flush=True)
        except Exception as e:
            print(f"[nexus_debug_http] startup failed: {type(e).__name__}: {e}", flush=True)

    asyncio.create_task(_runner())
