"""
Nexus eyes — read-only introspection endpoints bolted onto nexus_debug_http.

Purpose: let an outside process (claude, the user, a supervisor script) see
live server state without screenshots. Everything here is read-only, best-
effort, wrapped — one endpoint failing never takes down the others.

Endpoints added to http://127.0.0.1:18789:
    GET /chat?ch=<name|id>&n=20
    GET /channels
    GET /members?status=online|offline|all
    GET /voice
    GET /chimes?n=20
    GET /caretaker
    GET /followups
    GET /memory?user=<name>&n=20
    GET /thoughts?n=10

All responses are JSON shaped as:
    {"ok": true,  "data": ...}
    {"ok": false, "error": "..."}
Always HTTP 200 — errors live in the body, not the status line.

Install:
    import nexus_eyes
    nexus_eyes.install(bot)   # call in on_ready AFTER nexus_debug_http.install(bot)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

import config
import nexus_debug_http


# ---------------------------------------------------------------------------
# Logging + bot handle
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_eyes] {msg}", flush=True)


_bot = None  # set by install()

ROOT = Path(config.ROOT) if hasattr(config, "ROOT") else Path(__file__).parent
LOG_FILE = ROOT / "nexus_bot.log"
CARETAKER_STATE = ROOT / "caretaker_state.json"
FOLLOWUPS_STATE = ROOT / "followups_state.json"
MIND_STATE = ROOT / "mind_state.json"


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _ok(data: Any) -> web.Response:
    return web.json_response({"ok": True, "data": data})


def _err(msg: str) -> web.Response:
    # Always 200 — errors travel in the body
    return web.json_response({"ok": False, "error": str(msg)[:500]})


def _iso(d) -> Optional[str]:
    try:
        if d is None:
            return None
        if isinstance(d, (int, float)):
            return dt.datetime.fromtimestamp(d, tz=dt.timezone.utc).isoformat(timespec="seconds")
        if hasattr(d, "isoformat"):
            return d.isoformat(timespec="seconds") if hasattr(d, "tzinfo") else d.isoformat()
    except Exception:
        return None
    return str(d)


# ---------------------------------------------------------------------------
# Channel matching: name (case-insensitive, emoji-stripped) OR snowflake id
# ---------------------------------------------------------------------------
# strip leading emoji/separator/punct so "💭│thoughts" matches "thoughts"
_LEAD_STRIP = re.compile(
    r"^[\s_\-\u00a0\u2000-\u206F\u2E00-\u2E7F\u2500-\u257F"
    r"\u2600-\u27BF\U0001F000-\U0001FFFF│|·•\.:,;]+",
    flags=re.UNICODE,
)


def _canon(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = _LEAD_STRIP.sub("", s)
    return s.strip()


def _first_guild():
    if _bot is None:
        return None
    try:
        return next(iter(_bot.guilds), None)
    except Exception:
        return None


def _resolve_channel(needle: str):
    """Resolve needle to a channel by snowflake id or by canon(name).
    Searches text AND voice channels across all guilds."""
    if _bot is None or not needle:
        return None
    # Try snowflake id
    if needle.isdigit():
        try:
            ch = _bot.get_channel(int(needle))
            if ch is not None:
                return ch
        except Exception:
            pass
    target = _canon(needle)
    if not target:
        return None
    for g in getattr(_bot, "guilds", []) or []:
        # text first
        for ch in getattr(g, "text_channels", []) or []:
            if _canon(ch.name) == target:
                return ch
        for ch in getattr(g, "voice_channels", []) or []:
            if _canon(ch.name) == target:
                return ch
        # substring fallback
        for ch in getattr(g, "text_channels", []) or []:
            if target in _canon(ch.name):
                return ch
        for ch in getattr(g, "voice_channels", []) or []:
            if target in _canon(ch.name):
                return ch
    return None


def _resolve_member_by_name(name: str):
    """Resolve a guild member by display_name or username (case-insensitive)."""
    if _bot is None or not name:
        return None
    needle = name.strip().lower()
    if not needle:
        return None
    # id path
    if needle.isdigit():
        for g in _bot.guilds:
            m = g.get_member(int(needle))
            if m is not None:
                return m
    for g in _bot.guilds:
        for m in getattr(g, "members", []) or []:
            if m.bot:
                continue
            if str(m.id) == needle:
                return m
            dn = (getattr(m, "display_name", "") or "").lower()
            un = (getattr(m, "name", "") or "").lower()
            if dn == needle or un == needle:
                return m
        # substring pass
        for m in getattr(g, "members", []) or []:
            if m.bot:
                continue
            dn = (getattr(m, "display_name", "") or "").lower()
            un = (getattr(m, "name", "") or "").lower()
            if needle in dn or needle in un:
                return m
    return None


# ---------------------------------------------------------------------------
# Log tail helper (line-based, last-N matching a predicate)
# ---------------------------------------------------------------------------
def _tail_matching(pattern: re.Pattern, n: int, max_scan_lines: int = 20000) -> list[str]:
    """Return up to N most recent lines matching pattern, from the tail of the log."""
    if not LOG_FILE.exists():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    all_lines = text.splitlines()
    scan = all_lines[-max_scan_lines:]
    out = [ln for ln in scan if pattern.search(ln)]
    return out[-n:]


# ===========================================================================
# /chat?ch=<name|id>&n=20
# ===========================================================================
async def _chat(req):
    try:
        ch_q = (req.query.get("ch") or "").strip()
        if not ch_q:
            return _err("missing 'ch' query param")
        try:
            n = int(req.query.get("n", "20"))
        except ValueError:
            n = 20
        n = max(1, min(n, 100))

        channel = _resolve_channel(ch_q)
        if channel is None:
            return _err(f"channel not found: {ch_q!r}")
        if not hasattr(channel, "history"):
            return _err(f"channel {ch_q!r} is not message-readable")

        msgs: list[dict] = []
        try:
            async for msg in channel.history(limit=n):
                ref_id = None
                try:
                    if msg.reference is not None:
                        ref_id = str(getattr(msg.reference, "message_id", "") or "")
                except Exception:
                    ref_id = None
                msgs.append({
                    "ts": _iso(msg.created_at),
                    "author": getattr(msg.author, "display_name", str(msg.author)),
                    "author_id": str(getattr(msg.author, "id", "")),
                    "content": (msg.content or "")[:2000],
                    "reply_to_id": ref_id,
                    "message_id": str(msg.id),
                })
        except Exception as e:
            return _err(f"history read failed: {type(e).__name__}: {e}")

        msgs.reverse()  # oldest first
        return _ok({
            "channel": {
                "id": str(channel.id),
                "name": getattr(channel, "name", "?"),
            },
            "count": len(msgs),
            "messages": msgs,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /channels
# ===========================================================================
async def _channels(_req):
    try:
        guild = _first_guild()
        if guild is None:
            return _err("no guild")

        out: list[dict] = []
        for ch in getattr(guild, "text_channels", []) or []:
            cat = getattr(ch.category, "name", None) if getattr(ch, "category", None) else None
            last_ts = None
            try:
                lm_id = getattr(ch, "last_message_id", None)
                if lm_id:
                    # derive ts from snowflake (Discord epoch 2015-01-01)
                    discord_epoch = 1420070400000
                    ts_ms = (int(lm_id) >> 22) + discord_epoch
                    last_ts = dt.datetime.fromtimestamp(
                        ts_ms / 1000, tz=dt.timezone.utc
                    ).isoformat(timespec="seconds")
            except Exception:
                last_ts = None
            out.append({
                "id": str(ch.id),
                "name": ch.name,
                "type": "text",
                "category": cat,
                "last_message_ts": last_ts,
                "member_count": None,  # n/a for text
                "position": getattr(ch, "position", 0),
            })
        for ch in getattr(guild, "voice_channels", []) or []:
            cat = getattr(ch.category, "name", None) if getattr(ch, "category", None) else None
            members = [m for m in (getattr(ch, "members", []) or []) if not m.bot]
            out.append({
                "id": str(ch.id),
                "name": ch.name,
                "type": "voice",
                "category": cat,
                "last_message_ts": None,
                "member_count": len(members),
                "position": getattr(ch, "position", 0),
            })

        # Sort: category (None last), then position, then name
        def _sort_key(c):
            return (c["category"] is None, c["category"] or "", c["position"], c["name"])

        out.sort(key=_sort_key)
        return _ok({"guild": {"id": str(guild.id), "name": guild.name}, "channels": out})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /members?status=online|offline|all
# ===========================================================================
async def _members(req):
    try:
        guild = _first_guild()
        if guild is None:
            return _err("no guild")
        status_q = (req.query.get("status") or "online").lower()
        if status_q not in ("online", "offline", "all"):
            status_q = "online"

        # Build voice-channel membership map
        in_voice: dict[int, str] = {}
        for vc in getattr(guild, "voice_channels", []) or []:
            for m in getattr(vc, "members", []) or []:
                in_voice[m.id] = vc.name

        rows: list[dict] = []
        for m in getattr(guild, "members", []) or []:
            if m.bot:
                continue
            status_str = str(getattr(m, "status", "offline"))
            if status_q == "online" and status_str == "offline":
                continue
            if status_q == "offline" and status_str != "offline":
                continue
            roles = [r.name for r in getattr(m, "roles", []) or [] if r.name != "@everyone"]
            rows.append({
                "id": str(m.id),
                "name": getattr(m, "name", ""),
                "display_name": getattr(m, "display_name", ""),
                "status": status_str,
                "roles": roles,
                "in_voice_channel": in_voice.get(m.id),
            })
        rows.sort(key=lambda r: (r["status"] == "offline", r["display_name"].lower()))
        return _ok({
            "guild": {"id": str(guild.id), "name": guild.name},
            "status_filter": status_q,
            "count": len(rows),
            "members": rows,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /voice
# ===========================================================================
async def _voice(_req):
    try:
        guild = _first_guild()
        if guild is None:
            return _err("no guild")
        vc = getattr(guild, "voice_client", None)
        if vc is None or not getattr(vc, "is_connected", lambda: False)():
            # Still try to surface watcher state even when not connected
            opening = _opening_watcher_snapshot()
            out = {"connected": False, "vc_name": None, "members": []}
            if opening is not None:
                out["opening_watcher_active"] = opening
            return _ok(out)

        ch = getattr(vc, "channel", None)
        members = []
        for m in (getattr(ch, "members", []) if ch else []) or []:
            if m.bot:
                continue
            speaking = False
            try:
                spk = getattr(vc, "get_speaking", None)
                if callable(spk):
                    speaking = bool(spk(m))
            except Exception:
                speaking = False
            members.append({
                "id": str(m.id),
                "name": getattr(m, "display_name", str(m)),
                "speaking": speaking,
            })

        out = {
            "connected": True,
            "vc_name": getattr(ch, "name", None),
            "vc_id": str(getattr(ch, "id", "")) if ch else None,
            "members": members,
            "listening": bool(getattr(vc, "is_listening", lambda: False)()),
            "playing": bool(getattr(vc, "is_playing", lambda: False)()),
        }
        opening = _opening_watcher_snapshot()
        if opening is not None:
            out["opening_watcher_active"] = opening
        return _ok(out)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def _opening_watcher_snapshot() -> Optional[dict]:
    """Read opening-watcher state from nexus_listen if importable."""
    try:
        import nexus_listen
        # These are module-private names. Wrap in try so we don't break if they move.
        started = bool(getattr(nexus_listen, "_opening_watcher_started", False))
        handlers = getattr(nexus_listen, "_opening_handlers", []) or []
        tracked = dict(getattr(nexus_listen, "_last_substantive_ts", {}) or {})
        return {
            "started": started,
            "handler_count": len(handlers),
            "tracked_channels": {str(k): _iso(v) for k, v in tracked.items()},
        }
    except Exception:
        return None


# ===========================================================================
# /chimes?n=20
# ===========================================================================
_CHIME_RE = re.compile(
    r"\[nexus_proactive\]\s+chimed\s+kind=(?P<kind>\S+)\s+ch=(?P<ch>\S+)\s+"
    r"len=(?P<length>\d+)\s+preview=(?P<preview>.*)$"
)
_TS_LEAD_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[\.,]?\d*)")


def _parse_chime_line(line: str) -> Optional[dict]:
    m = _CHIME_RE.search(line)
    if not m:
        return None
    ts_m = _TS_LEAD_RE.search(line)
    preview = m.group("preview").strip()
    # Strip surrounding quotes from !r style repr
    if len(preview) >= 2 and preview[0] in ("'", '"') and preview[-1] == preview[0]:
        preview = preview[1:-1]
    try:
        length = int(m.group("length"))
    except Exception:
        length = None
    return {
        "ts": ts_m.group("ts") if ts_m else None,
        "kind": m.group("kind"),
        "channel": m.group("ch"),
        "length": length,
        "preview": preview[:300],
    }


async def _chimes(req):
    try:
        try:
            n = int(req.query.get("n", "20"))
        except ValueError:
            n = 20
        n = max(1, min(n, 200))
        lines = _tail_matching(re.compile(r"\[nexus_proactive\]\s+chimed\s+kind="), n)
        events = []
        for ln in lines:
            parsed = _parse_chime_line(ln)
            if parsed is not None:
                events.append(parsed)
            else:
                events.append({"raw": ln[:500]})
        return _ok({"count": len(events), "chimes": events})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /caretaker
# ===========================================================================
async def _caretaker(_req):
    try:
        state = {}
        if CARETAKER_STATE.exists():
            try:
                state = json.loads(CARETAKER_STATE.read_text(encoding="utf-8"))
            except Exception as e:
                state = {"_state_read_error": f"{type(e).__name__}: {e}"}

        check_lines = _tail_matching(
            re.compile(r"\[nexus_caretaker\]\s+check"), 20
        )
        next_cycle_lines = _tail_matching(
            re.compile(r"\[nexus_caretaker\]\s+next cycle in"), 3
        )

        return _ok({
            "state_file": str(CARETAKER_STATE),
            "state_exists": CARETAKER_STATE.exists(),
            "state": state,
            "recent_checks": check_lines,
            "next_cycle_log": next_cycle_lines,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /followups
# ===========================================================================
async def _followups(_req):
    try:
        state = {}
        if FOLLOWUPS_STATE.exists():
            try:
                state = json.loads(FOLLOWUPS_STATE.read_text(encoding="utf-8"))
            except Exception as e:
                state = {"_state_read_error": f"{type(e).__name__}: {e}"}

        stats = None
        try:
            import nexus_followups
            stats = nexus_followups.get_stats()
        except Exception as e:
            stats = {"_get_stats_error": f"{type(e).__name__}: {e}"}

        return _ok({
            "state_file": str(FOLLOWUPS_STATE),
            "state_exists": FOLLOWUPS_STATE.exists(),
            "state": state,
            "stats": stats,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /memory?user=<name>&n=20
# ===========================================================================
async def _memory(req):
    try:
        user_q = (req.query.get("user") or "").strip()
        if not user_q:
            return _err("missing 'user' query param")
        try:
            n = int(req.query.get("n", "20"))
        except ValueError:
            n = 20
        n = max(1, min(n, 100))
        query_text = (req.query.get("q") or "").strip()

        member = _resolve_member_by_name(user_q)
        if member is None:
            return _err(f"user not found in guild: {user_q!r}")
        user_id = str(member.id)

        # Resolve mem0 client + lock from nexus_brain
        try:
            import nexus_brain
            mem = nexus_brain._get_mem0()
            lock = getattr(nexus_brain, "_MEM0_LOCK", None)
        except Exception as e:
            return _err(f"mem0 unavailable: {type(e).__name__}: {e}")

        # Run in a worker thread — mem0 does blocking IO + pyo3 bindings.
        # Hold _MEM0_LOCK around the search to match nexus_brain's convention.
        def _blocking_search() -> list:
            try:
                if lock is not None:
                    with lock:
                        if query_text:
                            results = mem.search(
                                query=query_text,
                                filters={"user_id": user_id},
                                limit=max(n * 2, 20),
                            )
                        else:
                            results = mem.get_all(filters={"user_id": user_id})
                else:
                    if query_text:
                        results = mem.search(
                            query=query_text,
                            filters={"user_id": user_id},
                            limit=max(n * 2, 20),
                        )
                    else:
                        results = mem.get_all(filters={"user_id": user_id})
                mems = results.get("results", []) if isinstance(results, dict) else results
                return list(mems or [])
            except Exception as e:
                raise e

        try:
            mems = await asyncio.to_thread(_blocking_search)
        except Exception as e:
            return _err(f"mem0 search failed: {type(e).__name__}: {e}")

        def _extract(m: dict) -> dict:
            meta = m.get("metadata") or {}
            return {
                "id": m.get("id") or m.get("memory_id"),
                "memory": m.get("memory") or m.get("text") or "",
                "metadata": meta,
                "created_at": m.get("created_at") or meta.get("created_at"),
                "user_id": m.get("user_id") or meta.get("user_id"),
            }

        # Sort newest first when we have a created_at
        def _sort_key(m):
            ca = m.get("created_at") or ""
            return str(ca)

        shaped = [_extract(m) for m in mems]
        shaped.sort(key=_sort_key, reverse=True)
        shaped = shaped[:n]

        return _ok({
            "user": {
                "id": user_id,
                "name": getattr(member, "name", ""),
                "display_name": getattr(member, "display_name", ""),
            },
            "query": query_text or None,
            "count": len(shaped),
            "memories": shaped,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# /thoughts?n=10
# ===========================================================================
async def _thoughts(req):
    try:
        try:
            n = int(req.query.get("n", "10"))
        except ValueError:
            n = 10
        n = max(1, min(n, 100))

        if MIND_STATE.exists():
            try:
                state = json.loads(MIND_STATE.read_text(encoding="utf-8"))
                return _ok({
                    "source": "mind_state.json",
                    "path": str(MIND_STATE),
                    "state": state,
                })
            except Exception as e:
                # fall through to log tail
                _log(f"mind_state read failed: {type(e).__name__}: {e}")

        posted = _tail_matching(
            re.compile(r"\[nexus_mind\]\s+posted"), n
        )
        # also surface the most recent nexus_mind lines of any kind for context
        all_mind = _tail_matching(re.compile(r"\[nexus_mind\]"), max(n, 20))
        return _ok({
            "source": "log_tail",
            "path": str(LOG_FILE),
            "posted_thoughts": posted,
            "recent_mind_log": all_mind[-n:],
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ===========================================================================
# Install
# ===========================================================================
_ROUTES = [
    ("GET", "/chat", _chat),
    ("GET", "/channels", _channels),
    ("GET", "/members", _members),
    ("GET", "/voice", _voice),
    ("GET", "/chimes", _chimes),
    ("GET", "/caretaker", _caretaker),
    ("GET", "/followups", _followups),
    ("GET", "/memory", _memory),
    ("GET", "/thoughts", _thoughts),
]


def install(bot) -> None:
    """Register all eye endpoints with nexus_debug_http. Idempotent."""
    global _bot
    _bot = bot
    if getattr(install, "_installed", False):
        _log("already installed, skipping")
        return
    install._installed = True

    registered = 0
    for method, path, handler in _ROUTES:
        try:
            nexus_debug_http.register_route(method, path, handler)
            registered += 1
        except Exception as e:
            _log(f"register {method} {path} failed: {type(e).__name__}: {e}")

    _log(f"installed {registered}/{len(_ROUTES)} endpoints on debug http")


__all__ = ["install"]
