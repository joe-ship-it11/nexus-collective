"""
Nexus skills / connector layer.

Turns Nexus from oracle into connector. Two halves:

  1. Extractor: detects skill / interest / role declarations in a message
     ("I do backend API stuff at work", "I'm a UX designer") and stores them
     in mem0 under scope="tnc", tag="skill". Runs after nexus_brain.remember().

  2. Connector lookup: given a question-shaped message, finds TNC members
     whose declared skills match. Used by nexus_proactive to route to
     "connect_to_member" chimes instead of answering directly.

Pure logic — no discord imports. Safe to await from any event loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

import anthropic

import nexus_brain


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------
SKILLS_MIN_CONFIDENCE = float(os.environ.get("SKILLS_MIN_CONFIDENCE", 0.7))
SKILLS_MIN_MESSAGE_CHARS = int(os.environ.get("SKILLS_MIN_MESSAGE_CHARS", 20))
SKILLS_TOPIC_CACHE_TTL_S = int(os.environ.get("SKILLS_TOPIC_CACHE_TTL_S", 300))
SKILLS_MODEL = os.environ.get("SKILLS_MODEL", "claude-haiku-4-5-20251001")
SKILLS_TOPIC_CACHE_MAX = 200
SKILLS_VERBOSE = bool(os.environ.get("SKILLS_VERBOSE"))

_PROFESSIONAL_BOOST = 0.2


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
# Anthropic client — created once
client = anthropic.Anthropic()

# Reuse the mem0 lock from nexus_brain if exposed; fall back to local lock.
try:
    _MEM0_LOCK = nexus_brain._MEM0_LOCK  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover — defensive fallback
    _MEM0_LOCK = threading.Lock()

# Topic cache: query_hash -> (expiry_ts, [topic, ...]). OrderedDict for LRU-ish eviction.
_topic_cache: "OrderedDict[str, tuple[float, list[str]]]" = OrderedDict()
_topic_cache_lock = threading.Lock()

_stats_lock = threading.Lock()
_stats = {
    "extract_calls": 0,
    "extract_skipped": 0,
    "skills_stored": 0,
    "lookup_calls": 0,
    "lookup_hits": 0,        # lookups that returned >=1 match
    "lookup_empty": 0,
    "topic_cache_hits": 0,
    "topic_cache_misses": 0,
}

_installed = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_skills] {msg}", flush=True)


def _dlog(msg: str) -> None:
    if SKILLS_VERBOSE:
        print(f"[nexus_skills] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Shape heuristics
# ---------------------------------------------------------------------------
# Question-shaped message: either contains a '?' or starts with a wh/help word.
_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(how|what|where|when|why|who|anyone|does anyone|can someone|"
    r"need help|stuck on|trying to|looking for|any tips|is there|should i|could i)"
    r"\b",
    re.IGNORECASE,
)

# Declaration-killer: clearly a question, not a declaration.
_QUESTION_SHAPE_RE = re.compile(
    r"^\s*(how do i|how do you|anyone know|what's the best|whats the best|"
    r"what is the best|can anyone|does anyone|any idea|anyone have|"
    r"how does|how can|where do i|where can i|help with)\b",
    re.IGNORECASE,
)


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    if _QUESTION_PREFIX_RE.match(t):
        return True
    return False


def _looks_like_pure_question(text: str) -> bool:
    """Strong question shape — skip extraction entirely."""
    t = (text or "").strip()
    if not t:
        return True
    if _QUESTION_SHAPE_RE.match(t):
        return True
    # If the whole message is a question (ends with ?), treat as ask not declaration
    if t.endswith("?") and not re.search(r"\bi\s+(do|am|work|build|design|code|make|love|like|use)\b", t, re.IGNORECASE):
        return True
    return False


# ---------------------------------------------------------------------------
# Topic cache helpers
# ---------------------------------------------------------------------------
def _cache_key(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()


def _topic_cache_get(key: str) -> Optional[list[str]]:
    now = time.time()
    with _topic_cache_lock:
        hit = _topic_cache.get(key)
        if not hit:
            return None
        exp, topics = hit
        if exp < now:
            _topic_cache.pop(key, None)
            return None
        # Refresh LRU position
        _topic_cache.move_to_end(key)
        return list(topics)


def _topic_cache_put(key: str, topics: list[str]) -> None:
    with _topic_cache_lock:
        _topic_cache[key] = (time.time() + SKILLS_TOPIC_CACHE_TTL_S, list(topics))
        _topic_cache.move_to_end(key)
        while len(_topic_cache) > SKILLS_TOPIC_CACHE_MAX:
            _topic_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Haiku prompts
# ---------------------------------------------------------------------------
_EXTRACT_SYSTEM = """you detect skill / interest / role declarations in a user's message.

output JSON ONLY — no prose, no markdown:
{"skills": [{"skill": "<short canonical noun phrase, lowercase>", "evidence": "<the user's exact phrase>", "kind": "professional|hobby|interest|tool", "confidence": 0.0-1.0}]}

a "skill" is something the user IS / DOES / USES / IS INTO. it must be declared, not asked about.

examples that ARE skills:
- "I do backend API stuff at work"        -> {skill: "backend api development", kind: "professional", conf: 0.9}
- "been getting into ableton lately"      -> {skill: "ableton / music production", kind: "hobby", conf: 0.85}
- "I'm a UX designer"                     -> {skill: "ux design", kind: "professional", conf: 0.95}
- "I use blender for 3d modeling"         -> {skill: "blender / 3d modeling", kind: "tool", conf: 0.85}
- "I'm deep into rust"                    -> {skill: "rust", kind: "interest", conf: 0.8}

examples that are NOT skills (return empty list):
- "I think python is cool"                -> opinion, not a declaration of use
- "anyone know react?"                    -> question, an ask not a declaration
- "I went to the store"                   -> event, not a skill
- "that's dope"                           -> vibe, not a skill
- "I want to learn golang someday"        -> aspiration, not current

rules:
- lowercase the skill name
- prefer canonical noun phrases ("backend api development", not "doing backend API stuff at work")
- evidence must be a VERBATIM quote from the message
- confidence below 0.7 means you're not sure it's a real declaration — include it with low conf, caller filters
- if nothing qualifies: {"skills": []}"""


_TOPIC_SYSTEM = """extract 1-3 topic noun phrases from a user's question so we can search for members who know about those topics.

output JSON ONLY:
{"topics": ["<noun phrase>", ...]}

rules:
- lowercase
- noun phrases only, no verbs ("api design" not "designing apis")
- prefer the CORE technical/creative topic, not filler ("react performance" not "help")
- 1-3 topics max, sorted most-relevant first
- if the message is vague or non-technical: {"topics": []}

examples:
- "anyone know how to debounce a react hook?" -> {"topics": ["react hooks", "debounce"]}
- "how do I set up ableton for live performance?" -> {"topics": ["ableton live performance"]}
- "what's the vibe tonight" -> {"topics": []}"""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------
def _parse_json_payload(raw: str) -> Optional[dict]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip("` \n")
    try:
        return json.loads(s)
    except Exception:
        return None


def _run_haiku(system: str, user: str, max_tokens: int, temperature: float) -> Optional[str]:
    """Sync Haiku call. Returns raw text or None on error."""
    try:
        resp = client.messages.create(
            model=SKILLS_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
    except Exception as e:
        _log(f"haiku call error ({type(e).__name__}): {e}")
        return None


def _skill_dedup_match(new_skill: str, existing_md_skill: str) -> bool:
    """Case-insensitive substring match either direction."""
    a = (new_skill or "").strip().lower()
    b = (existing_md_skill or "").strip().lower()
    if not a or not b:
        return False
    return a in b or b in a


def _existing_skills_for_user(m, user_id: str, skill_query: str) -> list[dict]:
    """Fetch existing skill entries for this user for dedup. Returns raw mem0 list."""
    try:
        with _MEM0_LOCK:
            results = m.search(
                query=skill_query,
                user_id=user_id,
                filters={"AND": [{"tag": "skill"}]},
                limit=10,
            )
        mems = results.get("results", []) if isinstance(results, dict) else results
        return mems or []
    except Exception as e:
        _dlog(f"dedup search error ({type(e).__name__}): {e}")
        return []


async def extract_from_message(user_id: str, user_name: str, message: str) -> int:
    """
    Hook called from nexus_brain.remember() AFTER the existing mem0 write.
    Runs a Haiku call to detect skill/interest/role declarations.
    Returns count of skills stored. Fire-and-forget from caller — never raises.
    """
    try:
        with _stats_lock:
            _stats["extract_calls"] += 1

        text = (message or "").strip()
        if len(text) < SKILLS_MIN_MESSAGE_CHARS:
            with _stats_lock:
                _stats["extract_skipped"] += 1
            _dlog(f"extract skip (too short): {len(text)}<{SKILLS_MIN_MESSAGE_CHARS}")
            return 0

        if _looks_like_pure_question(text):
            with _stats_lock:
                _stats["extract_skipped"] += 1
            _dlog(f"extract skip (question shape): {text[:60]!r}")
            return 0

        raw = await asyncio.to_thread(
            _run_haiku,
            _EXTRACT_SYSTEM,
            f"message from {user_name}:\n{text[:1200]}",
            300,
            0.2,
        )
        if raw is None:
            return 0
        data = _parse_json_payload(raw)
        if not isinstance(data, dict):
            _dlog(f"extract: unparseable haiku output: {raw[:120]!r}")
            return 0

        skills = data.get("skills") or []
        if not isinstance(skills, list) or not skills:
            _dlog(f"extract: no skills detected in: {text[:60]!r}")
            return 0

        try:
            m = nexus_brain._get_mem0()  # type: ignore[attr-defined]
        except Exception as e:
            _log(f"extract: mem0 unavailable ({type(e).__name__}): {e}")
            return 0

        stored = 0
        now_iso = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        for entry in skills:
            if not isinstance(entry, dict):
                continue
            skill = str(entry.get("skill", "")).strip().lower()
            evidence = str(entry.get("evidence", "")).strip()
            kind = str(entry.get("kind", "")).strip().lower()
            try:
                conf = float(entry.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0

            if not skill or not evidence:
                continue
            if kind not in ("professional", "hobby", "interest", "tool"):
                kind = "interest"
            if conf < SKILLS_MIN_CONFIDENCE:
                _dlog(f"extract: skip low-conf {skill!r} conf={conf:.2f}")
                continue

            # Dedup: check if user already has a matching skill entry
            existing = _existing_skills_for_user(m, user_id, skill)
            duplicate = False
            for ex in existing:
                md = ex.get("metadata") or {}
                ex_skill = str(md.get("skill", ""))
                if _skill_dedup_match(skill, ex_skill):
                    duplicate = True
                    break
            if duplicate:
                _dlog(f"extract: dedup skip {user_name}/{skill!r}")
                continue

            # Write the skill memory
            try:
                # mem0 content — keep it human-readable; scope/tag live in metadata
                content = f"{user_name} — skill: {skill} ({kind}). evidence: \"{evidence}\""
                with _MEM0_LOCK:
                    m.add(
                        messages=[{"role": "user", "content": content}],
                        user_id=user_id,
                        agent_id="nexus",
                        metadata={
                            "user_name": user_name,
                            "scope": "tnc",
                            "tag": "skill",
                            "subtag": kind,
                            "skill": skill,
                            "evidence": evidence,
                            "kind": kind,
                            "confidence": conf,
                            "first_seen": now_iso,
                        },
                    )
                stored += 1
                with _stats_lock:
                    _stats["skills_stored"] += 1
                _log(f"stored skill: {user_name} / {skill} ({kind}) conf={conf:.2f}")
            except Exception as e:
                _log(f"extract: mem0.add failed for {skill!r} ({type(e).__name__}): {e}")

        return stored
    except Exception as e:
        _log(f"extract_from_message fatal ({type(e).__name__}): {e}")
        return 0


# ---------------------------------------------------------------------------
# Connector lookup
# ---------------------------------------------------------------------------
async def _extract_topics(query_text: str) -> list[str]:
    key = _cache_key(query_text)
    cached = _topic_cache_get(key)
    if cached is not None:
        with _stats_lock:
            _stats["topic_cache_hits"] += 1
        return cached

    with _stats_lock:
        _stats["topic_cache_misses"] += 1

    raw = await asyncio.to_thread(
        _run_haiku,
        _TOPIC_SYSTEM,
        (query_text or "").strip()[:800],
        80,
        0.0,
    )
    if raw is None:
        _topic_cache_put(key, [])
        return []
    data = _parse_json_payload(raw)
    topics: list[str] = []
    if isinstance(data, dict):
        raw_topics = data.get("topics") or []
        if isinstance(raw_topics, list):
            for t in raw_topics[:3]:
                if isinstance(t, str):
                    ts = t.strip().lower()
                    if ts:
                        topics.append(ts)
    _topic_cache_put(key, topics)
    return topics


def _score_entry(mem: dict) -> float:
    """Cosine-ish score from mem0 if present, else 1.0. Pro kind gets +0.2 boost."""
    raw = mem.get("score")
    try:
        score = float(raw) if raw is not None else 1.0
    except (TypeError, ValueError):
        score = 1.0
    md = mem.get("metadata") or {}
    kind = str(md.get("kind", "")).lower()
    if kind == "professional":
        score += _PROFESSIONAL_BOOST
    return score


async def skills_for_query(
    query_text: str,
    exclude_user_id: Optional[str] = None,
    limit: int = 3,
) -> list[dict]:
    """
    Given a just-sent message, find top N TNC members whose declared skills match.
    Returns list of dicts with user_id, user_name, skill, evidence, kind, score.
    Empty list if no good matches. Never raises.
    """
    try:
        with _stats_lock:
            _stats["lookup_calls"] += 1

        text = (query_text or "").strip()
        if not text:
            with _stats_lock:
                _stats["lookup_empty"] += 1
            return []

        # Heuristic gate — only run on question/help-seek shapes
        if not _looks_like_question(text):
            _dlog(f"lookup skip (not question shape): {text[:60]!r}")
            with _stats_lock:
                _stats["lookup_empty"] += 1
            return []

        topics = await _extract_topics(text)
        if not topics:
            _dlog(f"lookup skip (no topics): {text[:60]!r}")
            with _stats_lock:
                _stats["lookup_empty"] += 1
            return []

        try:
            m = nexus_brain._get_mem0()  # type: ignore[attr-defined]
        except Exception as e:
            _log(f"lookup: mem0 unavailable ({type(e).__name__}): {e}")
            return []

        # Search each topic server-wide, aggregate.
        # per-user best entry: user_id -> (score, entry_dict)
        best_by_user: dict[str, tuple[float, dict]] = {}

        for topic in topics:
            try:
                with _MEM0_LOCK:
                    results = m.search(
                        query=topic,
                        filters={"AND": [{"tag": "skill"}]},
                        limit=20,
                    )
                mems = results.get("results", []) if isinstance(results, dict) else results
                mems = mems or []
            except Exception as e:
                _dlog(f"lookup: mem0.search error for topic {topic!r} ({type(e).__name__}): {e}")
                continue

            for mem in mems:
                md = mem.get("metadata") or {}
                if str(md.get("tag", "")).lower() != "skill":
                    continue
                uid = str(mem.get("user_id") or md.get("user_id") or "")
                if not uid:
                    continue
                if exclude_user_id is not None and uid == str(exclude_user_id):
                    continue

                score = _score_entry(mem)
                prev = best_by_user.get(uid)
                if prev is None or score > prev[0]:
                    best_by_user[uid] = (score, {
                        "user_id": uid,
                        "user_name": str(md.get("user_name", "someone")),
                        "skill": str(md.get("skill", "")),
                        "evidence": str(md.get("evidence", "")),
                        "kind": str(md.get("kind", "")),
                        "score": score,
                    })

        if not best_by_user:
            _dlog(f"lookup: no matches for topics {topics}")
            with _stats_lock:
                _stats["lookup_empty"] += 1
            return []

        ranked = sorted(best_by_user.values(), key=lambda d: d["score"], reverse=True)
        top = ranked[: max(1, int(limit))]

        with _stats_lock:
            _stats["lookup_hits"] += 1
        top_preview = top[0]
        _log(
            f"lookup hit: query={text[:60]!r} topics={topics} "
            f"top={top_preview['user_name']}/{top_preview['skill']} "
            f"score={top_preview['score']:.2f} total_matches={len(ranked)}"
        )
        return top
    except Exception as e:
        _log(f"skills_for_query fatal ({type(e).__name__}): {e}")
        return []


# ---------------------------------------------------------------------------
# Helper for proactive prompt injection
# ---------------------------------------------------------------------------
def format_for_classifier(matches: list[dict]) -> str:
    """
    Render matches as a short bullet block for the proactive classifier's system prompt.
    Returns "" if empty.
    """
    if not matches:
        return ""
    lines = ["Members with relevant skills:"]
    for match in matches:
        name = str(match.get("user_name", "someone"))
        skill = str(match.get("skill", "")).strip()
        evidence = str(match.get("evidence", "")).strip()
        if not skill:
            continue
        if evidence:
            # Trim overly long evidence quotes
            if len(evidence) > 140:
                evidence = evidence[:137] + "…"
            lines.append(f"- {name}: {skill} (\"{evidence}\")")
        else:
            lines.append(f"- {name}: {skill}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats + install
# ---------------------------------------------------------------------------
def get_stats() -> dict:
    with _stats_lock:
        s = dict(_stats)
    total_topic = s["topic_cache_hits"] + s["topic_cache_misses"]
    hit_rate = (s["topic_cache_hits"] / total_topic) if total_topic else 0.0
    with _topic_cache_lock:
        cache_size = len(_topic_cache)
    return {
        "extract_calls": s["extract_calls"],
        "extract_skipped": s["extract_skipped"],
        "skills_stored": s["skills_stored"],
        "lookup_calls": s["lookup_calls"],
        "lookup_hits": s["lookup_hits"],
        "lookup_empty": s["lookup_empty"],
        "topic_cache_hits": s["topic_cache_hits"],
        "topic_cache_misses": s["topic_cache_misses"],
        "topic_cache_hit_rate": round(hit_rate, 3),
        "topic_cache_size": cache_size,
        "tunables": {
            "min_confidence": SKILLS_MIN_CONFIDENCE,
            "min_message_chars": SKILLS_MIN_MESSAGE_CHARS,
            "topic_cache_ttl_s": SKILLS_TOPIC_CACHE_TTL_S,
            "model": SKILLS_MODEL,
        },
    }


def install() -> None:
    """Idempotent install — logs the line once."""
    global _installed
    if _installed:
        return
    _installed = True
    _log(
        f"installed — model={SKILLS_MODEL} min_conf={SKILLS_MIN_CONFIDENCE} "
        f"min_chars={SKILLS_MIN_MESSAGE_CHARS} topic_ttl={SKILLS_TOPIC_CACHE_TTL_S}s"
    )


__all__ = [
    "extract_from_message",
    "skills_for_query",
    "format_for_classifier",
    "get_stats",
    "install",
    "SKILLS_MIN_CONFIDENCE",
    "SKILLS_MIN_MESSAGE_CHARS",
    "SKILLS_TOPIC_CACHE_TTL_S",
    "SKILLS_MODEL",
]
