"""
Transcript digest — pure counting/windowing over voice_transcripts.jsonl.

No LLM calls. No network. Safe to import unconditionally: if the transcript
file is missing or empty, every function returns an empty structure.

Each line of voice_transcripts.jsonl (append-only, written by nexus_listen):
    {"ts": <float unix>, "iso": <str>, "user_id": <str>, "name": <str>,
     "text": <str>, "dur_s": <float>, "triggered": <bool>}
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

TRANSCRIPTS_PATH: Path = config.ROOT / "voice_transcripts.jsonl"

# Crude stoplist — drop filler/common English so topic counts surface content words.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an and or but if then else for of to in on at by with without from as is are was were be been being
    am do does did doing done have has had having will would could should shall may might must can cannot
    not no yes the this that these those i you he she it we they them us him her me my mine your yours his
    hers its our ours their theirs what which who whom whose when where why how all any both each few more
    most other some such only own same so than too very just about against between into through during
    before after above below up down out off over under again further here there once also really even
    while back again still though although because since until upon onto off into within across behind
    like want wanted wants go goes going went come came comes coming get got gets getting know knows
    knew known make makes made making see saw seen sees look looks looked looking say said says saying
    think thinks thought thinking yeah yep yo ok okay um uh hmm hey dude bro lol nah huh ooh oh mm mhm
    gonna wanna gotta kinda sorta maybe probably actually basically literally pretty bit lot lots
    thing things stuff someone somebody something anyone anybody anything nobody nothing everybody
    everyone everything one two three four five six seven eight nine ten first second last next
    nexus
    """.split()
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}")


# ---------------------------------------------------------------------------
# Core IO
# ---------------------------------------------------------------------------
def _load_all() -> list[dict]:
    """Read every valid JSONL record. Missing/empty file → []. Never raises."""
    try:
        if not TRANSCRIPTS_PATH.exists():
            return []
    except Exception:
        return []

    records: list[dict] = []
    try:
        with TRANSCRIPTS_PATH.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                records.append(rec)
    except Exception:
        return []
    return records


def _ts(rec: dict) -> float:
    try:
        return float(rec.get("ts") or 0.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_recent_window(seconds: int = 120) -> list[dict]:
    """
    Return utterances from the last `seconds` seconds, sorted oldest → newest.
    Empty list if file missing or no matches.
    """
    if seconds <= 0:
        return []
    cutoff = time.time() - float(seconds)
    out = [r for r in _load_all() if _ts(r) >= cutoff]
    out.sort(key=_ts)
    return out


def get_user_summary(user_id: str, limit: int = 50) -> dict:
    """
    Per-user rollup from the most recent `limit` utterances for `user_id`.
    Empty dict structure if no matches.
    """
    empty = {
        "user_id": str(user_id) if user_id is not None else "",
        "name": "",
        "utterances": 0,
        "total_duration_s": 0.0,
        "avg_duration_s": 0.0,
        "trigger_count": 0,
        "first_ts": None,
        "last_ts": None,
        "samples": [],
    }
    if not user_id:
        return empty

    uid = str(user_id)
    recs = [r for r in _load_all() if str(r.get("user_id") or "") == uid]
    if not recs:
        return empty

    recs.sort(key=_ts)
    # Most recent `limit` — keep chronological order within that slice.
    if limit and limit > 0:
        recs = recs[-limit:]

    name = ""
    for r in reversed(recs):
        n = (r.get("name") or "").strip()
        if n:
            name = n
            break

    total_dur = 0.0
    trig = 0
    samples: list[str] = []
    for r in recs:
        try:
            total_dur += float(r.get("dur_s") or 0.0)
        except Exception:
            pass
        if bool(r.get("triggered")):
            trig += 1
        t = (r.get("text") or "").strip()
        if t:
            samples.append(t)

    n_utt = len(recs)
    return {
        "user_id": uid,
        "name": name,
        "utterances": n_utt,
        "total_duration_s": round(total_dur, 3),
        "avg_duration_s": round(total_dur / n_utt, 3) if n_utt else 0.0,
        "trigger_count": trig,
        "first_ts": _ts(recs[0]) or None,
        "last_ts": _ts(recs[-1]) or None,
        "samples": samples[-10:],
    }


def get_today_digest() -> dict:
    """
    Digest of everything spoken today (local time). Empty-shaped dict if no activity.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    empty = {
        "date": today_str,
        "utterances": 0,
        "unique_speakers": 0,
        "top_speakers": [],
        "trigger_count": 0,
        "longest_utterance": None,
    }

    all_recs = _load_all()
    if not all_recs:
        return empty

    today = datetime.now().date()
    todays: list[dict] = []
    for r in all_recs:
        ts = _ts(r)
        if not ts:
            continue
        try:
            if datetime.fromtimestamp(ts).date() == today:
                todays.append(r)
        except Exception:
            continue

    if not todays:
        return empty

    speaker_counts: Counter[str] = Counter()
    name_by_uid: dict[str, str] = {}
    trig = 0
    longest: Optional[dict] = None

    for r in todays:
        uid = str(r.get("user_id") or "")
        nm = (r.get("name") or "").strip() or uid or "unknown"
        if uid:
            name_by_uid[uid] = nm
        speaker_counts[nm] += 1
        if bool(r.get("triggered")):
            trig += 1
        try:
            d = float(r.get("dur_s") or 0.0)
        except Exception:
            d = 0.0
        if longest is None:
            longest = {"name": nm, "text": (r.get("text") or "").strip(), "dur_s": d}
        else:
            try:
                if d > float(longest.get("dur_s") or 0.0):
                    longest = {"name": nm, "text": (r.get("text") or "").strip(), "dur_s": d}
            except Exception:
                pass

    top_speakers = speaker_counts.most_common(5)
    unique = len({str(r.get("user_id") or r.get("name") or "") for r in todays if (r.get("user_id") or r.get("name"))})

    return {
        "date": today_str,
        "utterances": len(todays),
        "unique_speakers": unique,
        "top_speakers": top_speakers,
        "trigger_count": trig,
        "longest_utterance": longest,
    }


def top_topics(limit: int = 5) -> list[str]:
    """
    Crude word-frequency topics over the last 24h of voice text, filtered
    against a common-word stoplist. Returns at most `limit` tokens,
    highest frequency first. Empty list if nothing to count.
    """
    if limit <= 0:
        return []
    cutoff = time.time() - 24 * 3600
    counts: Counter[str] = Counter()
    for r in _load_all():
        if _ts(r) < cutoff:
            continue
        text = (r.get("text") or "").lower()
        if not text:
            continue
        for w in _WORD_RE.findall(text):
            w = w.strip("'-").lower()
            if len(w) < 3:
                continue
            if w in _STOPWORDS:
                continue
            counts[w] += 1
    return [w for w, _c in counts.most_common(limit)]


def format_for_prompt(seconds: int = 120, max_lines: int = 20) -> str:
    """
    Plain-text recent voice window. Format: 'name: text' per line.
    Empty string if no recent voice or file missing.
    """
    try:
        recs = get_recent_window(seconds=seconds)
    except Exception:
        return ""
    if not recs:
        return ""
    if max_lines and max_lines > 0:
        recs = recs[-max_lines:]
    lines: list[str] = []
    for r in recs:
        name = (r.get("name") or "").strip() or "someone"
        text = (r.get("text") or "").strip()
        if not text:
            continue
        # Collapse whitespace — keep prompt lines clean.
        text = re.sub(r"\s+", " ", text)
        lines.append(f"{name}: {text}")
    return "\n".join(lines)
