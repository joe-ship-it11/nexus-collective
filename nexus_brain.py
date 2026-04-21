"""
Nexus brain — Claude API + Mem0 integration.

Two responsibilities:
  1. Generate replies using Claude + server context + retrieved memories.
  2. Store substantive messages as memories (per-user + per-channel tags).

Mem0 config: fully local. Anthropic Claude as the LLM backend for Mem0's
extraction step, sentence-transformers for embeddings, Chroma for vector store.
Zero external cost outside Anthropic API.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

import config

# Profile cache — silent profile builder
PROFILE_CACHE_DIR = config.ROOT / "profiles_cache"
PROFILE_CACHE_DIR.mkdir(exist_ok=True)
PROFILE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
PROFILE_MEMORY_DRIFT = 3  # rebuild if memory count grew by this much

_persona_cache: Optional[str] = None
_anthropic_client: Optional[Anthropic] = None
_mem0_client = None

# mem0 + chromadb's pyo3 bindings blow up under concurrent access from multiple
# threads (AttributeError: bindings, KeyError on collection path). The mind
# loop, voice transcript writer, and reply pipeline all hit it concurrently —
# this lock serializes ALL mem0 operations (add + search) so they can't race.
_MEM0_LOCK = threading.Lock()


def _get_persona() -> str:
    global _persona_cache
    if _persona_cache is None:
        _persona_cache = config.PERSONA_FILE.read_text(encoding="utf-8")
    return _persona_cache


def _get_anthropic() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _get_mem0():
    """Lazy-init Mem0 with local embeddings + Chroma, Anthropic as extraction LLM."""
    global _mem0_client
    if _mem0_client is not None:
        return _mem0_client

    from mem0 import Memory

    config.MEM0_DATA_DIR.mkdir(exist_ok=True)

    mem_config = {
        "llm": {
            "provider": "anthropic",
            "config": {
                "model": config.CLAUDE_MODEL,
                "api_key": os.environ["ANTHROPIC_API_KEY"],
                "max_tokens": 1024,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "tnc_nexus",
                "path": str(config.MEM0_DATA_DIR),
            },
        },
    }
    _mem0_client = Memory.from_config(mem_config)
    return _mem0_client


# ---------------------------------------------------------------------------
# Memory scope helpers
# ---------------------------------------------------------------------------
# mem0 metadata filtering is uneven across backends, so we pull wider then
# post-filter in python. Reliable + keeps the logic here (not buried in mem0).

def _mem_scope(mem: dict) -> str:
    md = mem.get("metadata") or {}
    return str(md.get("scope", "personal")).lower()


def _mem_user(mem: dict) -> str:
    # mem0 sometimes returns user_id at top level, sometimes in metadata
    return str(mem.get("user_id") or (mem.get("metadata") or {}).get("user_id") or "")


def _filter_visible_to(mems: list[dict], viewer_user_id: Optional[str]) -> list[dict]:
    """
    Apply the visibility rules:
      personal → only the owner sees it
      tnc      → any TNC member (we trust anyone in the server)
      public   → everyone
    If viewer_user_id is None, hide all personal entries (safest default).
    """
    out = []
    for m in mems:
        scope = _mem_scope(m)
        if scope == "public" or scope == "tnc":
            out.append(m)
        elif scope == "personal":
            if viewer_user_id and _mem_user(m) == str(viewer_user_id):
                out.append(m)
            # else: pretend it doesn't know
        else:
            # unknown scope → treat as personal (closed)
            if viewer_user_id and _mem_user(m) == str(viewer_user_id):
                out.append(m)
    return out


# ---------------------------------------------------------------------------
# Memory write — called by listener for every substantive message
# ---------------------------------------------------------------------------
def remember(user_id: str, user_name: str, channel: str, message: str) -> None:
    """Store a message as memory. Classifies scope + tag on write.

    Respects opt-out: users who have opted out of memory are never recorded.
    """
    if len(message.strip()) < config.MIN_MESSAGE_CHARS_FOR_MEMORY:
        return

    # Consent gate — hard skip if user opted out
    try:
        import nexus_consent
        if nexus_consent.is_opted_out(user_id):
            return
    except Exception as e:
        print(f"[nexus_brain.remember] consent check failed (proceeding cautiously): {e}")

    try:
        import nexus_classifier
        cls = nexus_classifier.classify(message)
    except Exception as e:
        print(f"[nexus_brain.remember] classifier failed, defaulting personal/other: {e}")
        cls = {"scope": "personal", "tag": "other"}

    try:
        m = _get_mem0()
        with _MEM0_LOCK:
            m.add(
                messages=[{"role": "user", "content": message}],
                user_id=user_id,
                agent_id="nexus",  # stamped so open (cross-user) search has a valid filter
                metadata={
                    "user_name": user_name,
                    "channel": channel,
                    "scope": cls["scope"],
                    "tag": cls["tag"],
                },
            )
    except Exception as e:
        print(f"[nexus_brain.remember] error: {type(e).__name__}: {e}")

    # Fire-and-forget: scan this message for follow-up-shaped utterances
    # (tests, deadlines, trips) and skill declarations ("I do X"). Both
    # extractors are async and fully self-defended; they write their own
    # mem0 entries with tag=followup / tag=skill.
    def _spawn_extractors():
        try:
            import asyncio as _asyncio
            try:
                import nexus_followups
                _asyncio.run(nexus_followups.extract_from_message(
                    user_id, user_name, channel, message
                ))
            except Exception as _e:
                print(f"[nexus_brain.remember] followups extract: {type(_e).__name__}: {_e}")
            try:
                import nexus_skills
                _asyncio.run(nexus_skills.extract_from_message(
                    user_id, user_name, message
                ))
            except Exception as _e:
                print(f"[nexus_brain.remember] skills extract: {type(_e).__name__}: {_e}")
        except Exception as _e:
            print(f"[nexus_brain.remember] extractor thread fatal: {type(_e).__name__}: {_e}")
    threading.Thread(target=_spawn_extractors, daemon=True).start()


# ---------------------------------------------------------------------------
# Memory read — retrieve relevant memories for a query
# ---------------------------------------------------------------------------
def recall(
    query: str,
    user_id: Optional[str] = None,
    top_k: int = None,
    viewer_user_id: Optional[str] = None,
) -> list[dict]:
    """
    Return top-K memories relevant to `query`.

    Args:
      user_id:        if set, scope search to this speaker's memories
      viewer_user_id: who is asking. Personal memories are only returned if
                      viewer_user_id == owner. When None, personal = hidden.
      top_k:          returned count (we over-pull internally then filter)
    """
    top_k = top_k or config.MEM0_TOP_K
    effective_viewer = viewer_user_id if viewer_user_id is not None else user_id
    try:
        m = _get_mem0()
        # Over-pull so scope filtering doesn't starve the result set
        limit = max(top_k * 3, 16)
        with _MEM0_LOCK:
            if user_id:
                results = m.search(query=query, filters={"user_id": user_id}, limit=limit)
            else:
                # mem0 requires at least one of user_id/agent_id/run_id.
                # We stamp agent_id="nexus" on every write, so filter on that for open (cross-user) search.
                results = m.search(query=query, filters={"agent_id": "nexus"}, limit=limit)
        mems = results.get("results", []) if isinstance(results, dict) else results
        mems = _filter_visible_to(mems or [], effective_viewer)
        return mems[:top_k]
    except Exception as e:
        print(f"[nexus_brain.recall] error: {type(e).__name__}: {e}")
        return []


def get_all_for_user(user_id: str, viewer_user_id: Optional[str] = None) -> list[dict]:
    """Dump every memory for a user (for /whoami). Respects scope visibility."""
    effective_viewer = viewer_user_id if viewer_user_id is not None else user_id
    try:
        m = _get_mem0()
        with _MEM0_LOCK:
            results = m.get_all(filters={"user_id": user_id})
        mems = results.get("results", []) if isinstance(results, dict) else results
        return _filter_visible_to(mems or [], effective_viewer)
    except Exception as e:
        print(f"[nexus_brain.get_all_for_user] error: {type(e).__name__}: {e}")
        return []


def forget_memory(memory_id: str) -> bool:
    """Delete a single memory by id. Returns True on success."""
    try:
        m = _get_mem0()
        with _MEM0_LOCK:
            m.delete(memory_id=memory_id)
        return True
    except Exception as e:
        print(f"[nexus_brain.forget_memory] error: {type(e).__name__}: {e}")
        return False


def forget_all_for_user(user_id: str) -> int:
    """Delete every memory belonging to this user. Returns count deleted."""
    deleted = 0
    try:
        m = _get_mem0()
        with _MEM0_LOCK:
            results = m.get_all(filters={"user_id": user_id})
        mems = results.get("results", []) if isinstance(results, dict) else results
        for mem in (mems or []):
            mid = mem.get("id") or mem.get("memory_id")
            if not mid:
                continue
            try:
                with _MEM0_LOCK:
                    m.delete(memory_id=mid)
                deleted += 1
            except Exception as e:
                print(f"[nexus_brain.forget_all_for_user] failed on {mid}: {e}")
    except Exception as e:
        print(f"[nexus_brain.forget_all_for_user] error: {type(e).__name__}: {e}")
    # Also clear their profile cache
    try:
        cache_path = PROFILE_CACHE_DIR / f"{user_id}.json"
        if cache_path.exists():
            cache_path.unlink()
    except Exception:
        pass
    return deleted


def update_scope(memory_id: str, new_scope: str) -> bool:
    """Manually relabel a memory's scope. Used by /mem scope slash command."""
    new_scope = (new_scope or "").lower()
    if new_scope not in ("personal", "tnc", "public"):
        return False
    try:
        m = _get_mem0()
        # mem0's update() takes metadata dict; we merge by re-fetching + patching
        with _MEM0_LOCK:
            existing = m.get(memory_id)
            md = dict((existing or {}).get("metadata") or {})
            md["scope"] = new_scope
            m.update(memory_id=memory_id, data=existing.get("memory", ""), metadata=md)
        return True
    except Exception as e:
        print(f"[nexus_brain.update_scope] error: {type(e).__name__}: {e}")
        return False


def summarize_user(user_name: str, memories: list[dict]) -> str:
    """
    Take a user's raw memory list, return a persona-voice profile.
    Short, sharp, punchy. Max 10 bullets. Honest about thin memory.
    """
    if not memories:
        return (
            f"{user_name.lower()} — memory's thin on you. haven't seen you say much that stuck yet. "
            f"talk more, say real things, come back and run this again."
        )

    # Build memory fragment list
    lines = []
    for i, mem in enumerate(memories[:40], 1):
        text = mem.get("memory") or mem.get("text") or str(mem)
        lines.append(f"- {text}")
    fragments = "\n".join(lines)

    persona = _get_persona()
    system = f"""{persona}

You are synthesizing {user_name}'s profile from your memory fragments of them.

Format:
- start with "{user_name.lower()} — here's what i've got."
- then 4-8 short bullet points (dashes, not numbered). facts only, pulled from memory.
- end with one observation or a question that invites them to tell you more.
- lowercase. terse. no fluff. no "as an AI". no disclaimers.
- if a fragment is thin or vague, don't pad it. less is more.
- never invent anything not in the fragments below.

memory fragments:
{fragments}
"""
    client = _get_anthropic()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": f"profile {user_name}."}],
    )
    out = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return out.strip()


# ---------------------------------------------------------------------------
# Silent profile builder — cached user profiles
# ---------------------------------------------------------------------------
def _profile_path(user_id: str) -> Path:
    safe = "".join(c for c in str(user_id) if c.isalnum())
    return PROFILE_CACHE_DIR / f"{safe}.json"


def _load_cached_profile(user_id: str) -> Optional[dict]:
    p = _profile_path(user_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_profile(user_id: str, user_name: str, profile: str, memory_count: int) -> None:
    p = _profile_path(user_id)
    try:
        p.write_text(
            json.dumps({
                "user_id": user_id,
                "user_name": user_name,
                "profile": profile,
                "memory_count": memory_count,
                "updated_ts": time.time(),
            }),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[nexus_brain._save_profile] error: {type(e).__name__}: {e}")


def _should_rebuild_profile(cached: Optional[dict], current_memory_count: int) -> bool:
    if not cached:
        return True
    age = time.time() - cached.get("updated_ts", 0)
    if age > PROFILE_TTL_SECONDS:
        return True
    drift = current_memory_count - cached.get("memory_count", 0)
    if drift >= PROFILE_MEMORY_DRIFT:
        return True
    return False


def get_or_build_profile(user_id: str, user_name: str, force: bool = False) -> str:
    """
    Return a cached compact profile for this user, rebuilding if stale.
    Called by /whoami AND by reply() for richer, consistent context.
    """
    memories = get_all_for_user(user_id)
    cached = _load_cached_profile(user_id)
    if not force and not _should_rebuild_profile(cached, len(memories)):
        return cached["profile"]

    profile = summarize_user(user_name, memories)
    _save_profile(user_id, user_name, profile, len(memories))
    return profile


def get_profile_brief(user_id: str, user_name: str) -> str:
    """
    Short version for injection into reply system prompts.
    Falls back to empty string if nothing meaningful.
    """
    try:
        return get_or_build_profile(user_id, user_name)
    except Exception as e:
        print(f"[nexus_brain.get_profile_brief] error: {type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Channel pulse — synthesize "what's happening here"
# ---------------------------------------------------------------------------
def summarize_channel(channel_name: str, recent_msgs: list[dict]) -> str:
    """
    Given recent messages in a channel, return a persona-voice snapshot:
    who's active, what themes, what's unresolved. Short, terse, honest.
    """
    if not recent_msgs:
        return (
            f"#{channel_name} — dead air. nothing recent to read. "
            f"come back when there's actual signal."
        )

    lines = []
    for m in recent_msgs[-80:]:
        author = m.get("author", "?")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 240:
            content = content[:237] + "…"
        lines.append(f"{author}: {content}")
    transcript = "\n".join(lines)

    persona = _get_persona()
    system = f"""{persona}

You are reading the recent activity in #{channel_name} and synthesizing a pulse:
who's been active, what themes are alive, what's unresolved or about to surface.

Format:
- start with "#{channel_name} — pulse check."
- 4-7 short dashes. facts only, drawn from the transcript.
- name names. attribute themes to people.
- end with one observation or question — what's alive, what's about to happen.
- lowercase. terse. no fluff. no "as an AI". no disclaimers.
- if the channel is thin or one-sided, say that plainly.
- never invent names, projects, or threads not in the transcript.

transcript:
{transcript}
"""
    client = _get_anthropic()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": f"pulse check on #{channel_name}."}],
    )
    out = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return out.strip()


# ---------------------------------------------------------------------------
# Reply generation
# ---------------------------------------------------------------------------
def reply(
    user_name: str,
    user_message: str,
    recent_context: list[dict],
    user_id: Optional[str] = None,
) -> str:
    """
    Generate a Nexus reply.

    recent_context: list of {"author": str, "content": str} — recent channel messages
                    (most recent last), NOT including the triggering message
    user_message:   the message Nexus is replying to
    user_id:        discord user id of the speaker (scopes memory recall)
    """
    # Pull memories — scope to speaker (continuity) AND cross-user (threading magic)
    # viewer_user_id = user_id: speaker can see their own personal memories,
    # cross-user results auto-hide everyone else's personal entries.
    own_mems = recall(user_message, user_id=user_id, top_k=4, viewer_user_id=user_id) if user_id else []
    cross_mems = recall(user_message, user_id=None, top_k=6, viewer_user_id=user_id)

    # Dedupe: drop cross_mems that duplicate own_mems by text
    own_texts = {(m.get("memory") or m.get("text") or "") for m in own_mems}
    cross_mems = [m for m in cross_mems if (m.get("memory") or m.get("text") or "") not in own_texts]

    def _fmt(mems):
        lines = []
        for i, m in enumerate(mems, 1):
            text = m.get("memory") or m.get("text") or str(m)
            meta = m.get("metadata", {}) or {}
            who = meta.get("user_name", "someone")
            where = meta.get("channel", "")
            lines.append(f"  {i}. {who} in #{where}: {text}")
        return "\n".join(lines)

    memory_block = ""
    if own_mems:
        memory_block += f"## what {user_name} has said before:\n{_fmt(own_mems)}\n"
    if cross_mems:
        memory_block += f"## related threads from others in TNC:\n{_fmt(cross_mems)}"

    # Pull cached profile of the speaker — gives replies continuity
    profile_block = ""
    if user_id:
        profile = get_profile_brief(user_id, user_name)
        if profile and "memory's thin" not in profile.lower():
            profile_block = f"## what you know about {user_name}:\n{profile}"

    # Build context block — use the full list as-fetched (caller sizes it;
    # the catch-up path widens it when the user actually asks about chat).
    ctx_block = ""
    if recent_context:
        lines = [f"  {m['author']}: {m['content']}" for m in recent_context]
        ctx_block = (
            "## what just happened in this channel (most recent last — this IS your view of the chat):\n"
            + "\n".join(lines)
            + "\n\n(When someone asks you to 'check chat' or 'see what happened', the block above IS the recent chat. "
            "Use it directly. Never say 'I don't see recent chats' or 'I can't see chat history' — you can.)"
        )

    # Ambient voice awareness — pull the last 45min of VC transcript if any.
    # Wide window so questions like "what did we talk about earlier" work even
    # after the current call ends. max_lines caps prompt bloat in busy calls.
    voice_block = ""
    try:
        import transcript_digest
        voice_ctx = transcript_digest.format_for_prompt(seconds=45 * 60, max_lines=40)
        if voice_ctx:
            voice_block = f"## Recent voice in VC (last 45min):\n{voice_ctx}"
    except Exception:
        pass  # silent fail — never break text reply due to transcript issues

    persona = _get_persona()
    system = f"""{persona}

{profile_block}

{ctx_block}

{voice_block}

{memory_block}

Respond to {user_name}. Keep it short unless depth is asked for."""

    client = _get_anthropic()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    # Concatenate text blocks
    out = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return out.strip()
