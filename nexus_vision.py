"""
Nexus vision — let nexus actually see images posted in TNC discord.

Two modes, both driven by the orchestrator:

  intent="react"      → terse 1-2 sentence conversational reaction, like a
                        friend reacting in chat. Returns None if the image is
                        boring/unclear (model is prompted to emit literal
                        "SKIP", which we translate to None).

  intent="describe"   → factual 3-5 sentence description, always returns
                        something if an image is present. Used for explicit
                        "nexus what's in this image" style requests.

Pure logic — no discord dispatch. Safe to await from any event loop.

Design notes:
  - Anthropic SDK is sync; every call wrapped in asyncio.to_thread.
  - Image bytes never stored; only URLs handed to the vision API.
  - URL-keyed response cache with 1h TTL, guarded by threading.Lock — repeated
    asks about the same image within an hour reuse the prior response.
  - asyncio.CancelledError always re-raised.
  - Fire-and-forget at the outer layer: every vision call is try/except →
    log + return None. Never raises.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import logging
from typing import Optional

from anthropic import Anthropic

try:
    import discord  # type: ignore
except Exception:  # pragma: no cover — discord missing shouldn't crash import
    discord = None  # type: ignore


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-sonnet-4-6")
VISION_CACHE_TTL_S = int(os.environ.get("VISION_CACHE_TTL_S", 60 * 60))  # 1h
VISION_MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES", 3))
VISION_REACT_MAX_TOKENS = int(os.environ.get("VISION_REACT_MAX_TOKENS", 120))
VISION_DESCRIBE_MAX_TOKENS = int(os.environ.get("VISION_DESCRIBE_MAX_TOKENS", 320))
VISION_VERBOSE = os.environ.get("VISION_VERBOSE", "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
# URL response cache: url -> (expiry_ts, response_text)
_url_cache: dict[str, tuple[float, str]] = {}
_url_cache_lock = threading.Lock()

# Anthropic client (lazy — env may not be populated at import time)
_client: Optional[Anthropic] = None
_client_lock = threading.Lock()

_installed = False


# ---------------------------------------------------------------------------
# Logging helpers — lowercase prefixed lines
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_vision] {msg}", flush=True)


def _dlog(msg: str) -> None:
    if VISION_VERBOSE:
        print(f"[nexus_vision] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Client accessor
# ---------------------------------------------------------------------------
def _get_client() -> Anthropic:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                # Anthropic() picks up ANTHROPIC_API_KEY from env
                _client = Anthropic()
    return _client


# ---------------------------------------------------------------------------
# URL cache helpers
# ---------------------------------------------------------------------------
def _cache_get(url: str) -> Optional[str]:
    now = time.time()
    with _url_cache_lock:
        hit = _url_cache.get(url)
        if not hit:
            return None
        expiry, text = hit
        if expiry < now:
            _url_cache.pop(url, None)
            return None
        return text


def _cache_put(url: str, text: str) -> None:
    expiry = time.time() + VISION_CACHE_TTL_S
    with _url_cache_lock:
        _url_cache[url] = (expiry, text)
        # Opportunistic prune — keep cache bounded
        if len(_url_cache) > 500:
            now = time.time()
            stale = [k for k, (exp, _) in _url_cache.items() if exp < now]
            for k in stale:
                _url_cache.pop(k, None)


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------
_IMAGE_URL_RE = re.compile(
    r"https?://\S+?\.(?:png|jpg|jpeg|webp|gif)(?:\?\S*)?",
    re.IGNORECASE,
)


def _extract_image_urls(message) -> list[str]:
    """Pull image URLs from attachments + inline links. Cap at VISION_MAX_IMAGES.

    Preserves order — attachments first, then in-text links. Dedups URLs while
    keeping first occurrence.
    """
    urls: list[str] = []
    seen: set[str] = set()

    # 1. Attachments — match on content_type OR filename extension
    # (Discord sometimes omits content_type on web-sourced uploads.)
    _IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".bmp")
    try:
        attachments = list(getattr(message, "attachments", []) or [])
    except Exception:
        attachments = []
    for att in attachments:
        try:
            ct = (getattr(att, "content_type", "") or "").lower()
            url = getattr(att, "url", None)
            fname = (getattr(att, "filename", "") or "").lower()
            is_image = ct.startswith("image/") or fname.endswith(_IMG_EXTS)
            if url and is_image and url not in seen:
                urls.append(url)
                seen.add(url)
                if len(urls) >= VISION_MAX_IMAGES:
                    return urls
        except Exception:
            continue

    # 2. Inline URLs in message.content
    try:
        content = str(getattr(message, "content", "") or "")
    except Exception:
        content = ""
    if content:
        for m in _IMAGE_URL_RE.finditer(content):
            url = m.group(0)
            # Strip common trailing punctuation that sometimes rides along
            url = url.rstrip(").,!?;:")
            if url not in seen:
                urls.append(url)
                seen.add(url)
                if len(urls) >= VISION_MAX_IMAGES:
                    break

    return urls


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_REACT_SYSTEM = (
    "you're a friend in a discord chat reacting to an image. one or two short "
    "sentences max. lowercase ok. if the image is boring or unclear, respond "
    "with the literal string 'SKIP'. don't describe what you see — react to it."
)

_DESCRIBE_SYSTEM = (
    "you are describing an image for someone who asked 'what's in this image?'. "
    "give a factual, neutral description in 3-5 sentences. cover the main "
    "subject, setting, and any notable details. no commentary, no reactions, "
    "no opinions. plain prose."
)


# ---------------------------------------------------------------------------
# Vision call
# ---------------------------------------------------------------------------
def _build_content_blocks(urls: list[str], intent: str) -> list[dict]:
    """Build the content array for the Anthropic messages.create call.

    Image blocks use source type 'url' — Anthropic's vision API accepts
    direct URLs (see anthropic docs: image block with source.type='url').
    """
    blocks: list[dict] = []
    for url in urls:
        blocks.append({
            "type": "image",
            "source": {"type": "url", "url": url},
        })
    # Trailing text prompt — nudges the model toward the right shape
    if intent == "describe":
        blocks.append({
            "type": "text",
            "text": "describe this image factually in 3-5 sentences.",
        })
    else:
        blocks.append({
            "type": "text",
            "text": "react to this image in one or two short sentences. if boring or unclear, respond with just 'SKIP'.",
        })
    return blocks


def _vision_call_sync(urls: list[str], intent: str) -> Optional[str]:
    """Sync Anthropic vision call. Returns raw text or None on error."""
    system = _DESCRIBE_SYSTEM if intent == "describe" else _REACT_SYSTEM
    max_tokens = (
        VISION_DESCRIBE_MAX_TOKENS if intent == "describe" else VISION_REACT_MAX_TOKENS
    )
    try:
        client = _get_client()
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=max_tokens,
            temperature=0.6 if intent == "react" else 0.2,
            system=system,
            messages=[{"role": "user", "content": _build_content_blocks(urls, intent)}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        return raw
    except Exception as e:
        _log(f"vision call error ({type(e).__name__}): {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def install(bot) -> None:
    """Idempotent install. Logs a line and preps the URL cache.

    `bot` is accepted for API symmetry with other modules; nothing is
    attached to it here (the orchestrator decides when to call us).
    """
    global _installed
    if _installed:
        return
    _installed = True
    # Prep / reset the cache (no-op if already empty, but explicit is nice)
    with _url_cache_lock:
        _url_cache.clear()
    _log(
        f"installed — model={VISION_MODEL} cache_ttl={VISION_CACHE_TTL_S}s "
        f"max_images={VISION_MAX_IMAGES} react_tokens={VISION_REACT_MAX_TOKENS} "
        f"describe_tokens={VISION_DESCRIBE_MAX_TOKENS} verbose={VISION_VERBOSE}"
    )


async def find_image_source(message, lookback: int = 8, max_age_s: int = 180):
    """Return a message with an image — either `message` itself, or a recent
    message from the SAME author within max_age_s. None if nothing found.

    This lets nexus follow multi-message patterns like:
        msg 1: "nexus" + <image>
        msg 2: "what do you see?"

    The current-message check and lookback are bundled here so every caller
    gets consistent behavior. Only same-author lookback to avoid picking up
    a random image someone else just posted.
    """
    try:
        # Current message first — fast path
        if _extract_image_urls(message):
            return message

        channel = getattr(message, "channel", None)
        if channel is None:
            return None

        import time as _t
        cutoff = _t.time() - max_age_s
        author_id = getattr(getattr(message, "author", None), "id", None)
        if author_id is None:
            return None

        async for prev in channel.history(limit=lookback):
            if prev.id == message.id:
                continue
            ca = getattr(prev, "created_at", None)
            if ca is not None and ca.timestamp() < cutoff:
                break
            if getattr(prev.author, "id", None) != author_id:
                continue
            if _extract_image_urls(prev):
                _dlog(f"lookback hit: image in msg {prev.id} ({int(_t.time() - ca.timestamp())}s ago)")
                return prev
    except Exception as e:
        _log(f"find_image_source error ({type(e).__name__}): {e}")
    return None


async def describe_message(message, intent: str = "react") -> Optional[str]:
    """
    Given a discord.Message with image attachments or image URLs in content,
    return a short text response (or None if no image / nothing worth saying).

    intent="react"     → terse conversational reaction (1-2 sentences).
                         Returns None if the model emits SKIP.
    intent="describe"  → factual 3-5 sentence description. Always returns
                         something if an image exists.

    Never raises. On any error, logs and returns None.
    """
    try:
        if intent not in ("react", "describe"):
            _dlog(f"unknown intent {intent!r}, defaulting to react")
            intent = "react"

        urls = _extract_image_urls(message)
        if not urls:
            _dlog("no image urls found")
            return None

        # Cache lookup — keyed on (url-tuple, intent) via a compound key so
        # react vs describe of the same image don't collide.
        cache_key = f"{intent}::" + "|".join(urls)
        cached = _cache_get(cache_key)
        if cached is not None:
            _dlog(f"cache hit intent={intent} urls={len(urls)}")
            if intent == "react" and cached.strip().upper() == "SKIP":
                return None
            return cached

        _dlog(f"vision call intent={intent} urls={len(urls)}")
        try:
            raw = await asyncio.to_thread(_vision_call_sync, urls, intent)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"to_thread dispatch error ({type(e).__name__}): {e}")
            return None

        if raw is None:
            return None

        # Store raw response (including any "SKIP") so a re-ask within TTL is free
        _cache_put(cache_key, raw)

        if intent == "react":
            cleaned = raw.strip().strip("`\"' \n")
            if cleaned.upper() == "SKIP" or cleaned.upper().startswith("SKIP"):
                _dlog("model skipped (boring/unclear)")
                return None
            _log(
                f"react produced urls={len(urls)} "
                f"preview={cleaned[:80]!r}"
            )
            return cleaned[:500]

        # describe intent
        cleaned = raw.strip().strip("`\"' \n")
        if not cleaned:
            return None
        _log(
            f"describe produced urls={len(urls)} "
            f"preview={cleaned[:80]!r}"
        )
        return cleaned[:2000]

    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"describe_message fatal ({type(e).__name__}): {e}")
        return None


def get_stats() -> dict:
    """Snapshot of vision module state for /diag visibility."""
    now = time.time()
    with _url_cache_lock:
        total = len(_url_cache)
        fresh = sum(1 for exp, _ in _url_cache.values() if exp >= now)
    return {
        "cache_entries_total": total,
        "cache_entries_fresh": fresh,
        "tunables": {
            "model": VISION_MODEL,
            "cache_ttl_s": VISION_CACHE_TTL_S,
            "max_images": VISION_MAX_IMAGES,
            "react_max_tokens": VISION_REACT_MAX_TOKENS,
            "describe_max_tokens": VISION_DESCRIBE_MAX_TOKENS,
            "verbose": VISION_VERBOSE,
        },
    }


__all__ = [
    "install",
    "describe_message",
    "get_stats",
    "VISION_MODEL",
    "VISION_CACHE_TTL_S",
    "VISION_MAX_IMAGES",
    "VISION_REACT_MAX_TOKENS",
    "VISION_DESCRIBE_MAX_TOKENS",
    "VISION_VERBOSE",
]
