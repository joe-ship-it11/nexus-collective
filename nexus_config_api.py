"""Live config API — GET/POST /config on the debug HTTP plane.

Lets the user (or Claude) read and tune module-level constants across
the Nexus codebase without restarting the bot. Live-imported readers
that reference these constants by module.attr (not via `from ... import
NAME`) will pick up the new value on next access.

Endpoints (registered on nexus_debug_http at install time):
    GET  /config                    -> {ok: true, data: {KEY: value, ...}}
    POST /config  body {KEY: value} -> {ok: true, applied: {...}, errors: {...}}

Type-coerces string JSON values to the registered type (int, float, bool,
str) so `curl -d '{"MIN_CONF":"0.9"}'` works alongside a proper
`{"MIN_CONF":0.9}`. Per-key atomic: one bad key never blocks others.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

from aiohttp import web


log = logging.getLogger("nexus_config_api")


def _log(msg: str) -> None:
    # Match project convention: lowercase, prefixed, print+log.
    line = f"[nexus_config_api] {msg}"
    print(line, flush=True)
    try:
        log.info(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Validators (return None on ok, str reason on failure)
# ---------------------------------------------------------------------------
def _range_int(lo: int, hi: int) -> Callable[[Any], Optional[str]]:
    def _v(x: Any) -> Optional[str]:
        try:
            n = int(x)
        except (TypeError, ValueError):
            return f"not an int: {x!r}"
        if n < lo or n > hi:
            return f"out of range [{lo}..{hi}]: {n}"
        return None
    return _v


def _range_float(lo: float, hi: float) -> Callable[[Any], Optional[str]]:
    def _v(x: Any) -> Optional[str]:
        try:
            f = float(x)
        except (TypeError, ValueError):
            return f"not a float: {x!r}"
        if f < lo or f > hi:
            return f"out of range [{lo}..{hi}]: {f}"
        return None
    return _v


# ---------------------------------------------------------------------------
# Registry — (module_dotted_name, attr_name, type_class, validator or None)
# ---------------------------------------------------------------------------
TUNABLES: list[tuple[str, str, type, Optional[Callable[[Any], Optional[str]]]]] = [
    # nexus_continuation — reply-window "still talking" seconds
    ("nexus_continuation", "DEFAULT_WINDOW_S", int, _range_int(5, 600)),

    # nexus_quotes — quote-book tuning
    ("nexus_quotes", "MIN_CONF", float, _range_float(0.0, 1.0)),
    ("nexus_quotes", "PER_USER_DAILY_MAX", int, _range_int(0, 50)),
    ("nexus_quotes", "PER_SERVER_DAILY_MAX", int, _range_int(0, 200)),
    ("nexus_quotes", "USER_COOLDOWN_S", int, _range_int(0, 86400)),

    # nexus_digest — morning briefing timing
    ("nexus_digest", "POST_HOUR", int, _range_int(0, 23)),
    ("nexus_digest", "POST_WINDOW_MIN", int, _range_int(1, 1440)),
    ("nexus_digest", "MIN_HOURS_BETWEEN", int, _range_int(1, 168)),

    # nexus_caretaker — background admin loop
    ("nexus_caretaker", "CARETAKER_INTERVAL_S", int, _range_int(60, 7200)),
    ("nexus_caretaker", "DEAD_CHANNEL_DAYS", int, _range_int(1, 30)),

    # nexus_vision — image-understanding cache TTL
    # NOTE: actual name in the module is VISION_CACHE_TTL_S, not CACHE_TTL_S.
    ("nexus_vision", "VISION_CACHE_TTL_S", int, _range_int(60, 86400)),

    # nexus_feedback — reaction learning stamp retention
    ("nexus_feedback", "STAMP_MAX_AGE_S", int, _range_int(3600, 30 * 24 * 3600)),
    ("nexus_feedback", "STAMP_MAX_COUNT", int, _range_int(100, 100000)),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_BOOL_TRUE = {"1", "true", "yes", "on", "y", "t"}
_BOOL_FALSE = {"0", "false", "no", "off", "n", "f"}


def _coerce(value: Any, t: type) -> Any:
    """Coerce a JSON-ish value to the registered type. Raises ValueError on miss."""
    if t is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _BOOL_TRUE:
                return True
            if s in _BOOL_FALSE:
                return False
            raise ValueError(f"cannot coerce {value!r} to bool")
        raise ValueError(f"cannot coerce {type(value).__name__} to bool")
    if t is int:
        if isinstance(value, bool):
            # bool is subclass of int — reject to avoid silent True->1
            raise ValueError("expected int, got bool")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise ValueError(f"expected int, got non-integer float {value}")
        if isinstance(value, str):
            return int(value.strip())
        raise ValueError(f"cannot coerce {type(value).__name__} to int")
    if t is float:
        if isinstance(value, bool):
            raise ValueError("expected float, got bool")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        raise ValueError(f"cannot coerce {type(value).__name__} to float")
    if t is str:
        if isinstance(value, str):
            return value
        return str(value)
    # Fallback — try direct construction
    return t(value)


def _lookup(key: str) -> Optional[tuple[str, str, type, Optional[Callable]]]:
    for entry in TUNABLES:
        if entry[1] == key:
            return entry
    return None


def _get_module(mod_name: str):
    """Import-or-return the cached module object so setattr hits the live one."""
    import sys as _sys
    if mod_name in _sys.modules:
        return _sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _read_all() -> dict:
    """Snapshot every registered tunable's current value."""
    out: dict = {}
    for mod_name, attr_name, t, _validator in TUNABLES:
        try:
            mod = _get_module(mod_name)
            val = getattr(mod, attr_name)
            out[attr_name] = {
                "value": val,
                "type": t.__name__,
                "module": mod_name,
            }
        except Exception as e:
            out[attr_name] = {
                "value": None,
                "type": t.__name__,
                "module": mod_name,
                "error": f"{type(e).__name__}: {e}",
            }
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def handle_get(_request: web.Request) -> web.Response:
    try:
        return web.json_response({"ok": True, "data": _read_all()})
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500,
        )


async def handle_post(request: web.Request) -> web.Response:
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
    if not body:
        return web.json_response(
            {"ok": True, "applied": {}, "errors": {}},
        )

    applied: dict = {}
    errors: dict = {}

    for raw_key, raw_val in body.items():
        key = str(raw_key)
        entry = _lookup(key)
        if entry is None:
            errors[key] = "not in tunables allow-list"
            continue
        mod_name, attr_name, t, validator = entry

        # Coerce
        try:
            coerced = _coerce(raw_val, t)
        except Exception as e:
            errors[key] = f"coerce failed: {e}"
            continue

        # Validate
        if validator is not None:
            try:
                why = validator(coerced)
            except Exception as e:
                why = f"validator error: {type(e).__name__}: {e}"
            if why:
                errors[key] = why
                continue

        # Apply
        try:
            mod = _get_module(mod_name)
            setattr(mod, attr_name, coerced)
            applied[key] = coerced
            _log(f"set {mod_name}.{attr_name} = {coerced!r}")
        except Exception as e:
            errors[key] = f"setattr failed: {type(e).__name__}: {e}"

    return web.json_response({"ok": True, "applied": applied, "errors": errors})


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
def install(bot=None) -> None:
    """Register /config routes on the debug HTTP server. Idempotent."""
    if getattr(install, "_installed", False):
        return
    install._installed = True  # type: ignore[attr-defined]

    try:
        import nexus_debug_http
    except Exception as e:
        _log(f"install failed — nexus_debug_http not importable: "
             f"{type(e).__name__}: {e}")
        return

    try:
        nexus_debug_http.register_route("GET", "/config", handle_get)
        nexus_debug_http.register_route("POST", "/config", handle_post)
    except Exception as e:
        _log(f"register_route failed: {type(e).__name__}: {e}")
        return

    _log(f"config api installed ({len(TUNABLES)} tunables)")


__all__ = ["install", "handle_get", "handle_post", "TUNABLES"]
