"""
Call summary — on /leave (or end of call), summarize the just-ended voice
call and write one memory entry per speaker so "what did we talk about
earlier" works across sessions / channels / days.

Reads voice_transcripts.jsonl (written by nexus_listen), pulls every line
from `since_iso` forward, groups utterances by speaker, asks Claude Haiku
for a terse summary, and writes per-speaker mem0 entries tagged
scope=tnc so anyone in the server can recall them via cross-user search.

Public API:
    summarize_and_store(since_iso: str) -> dict
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

import config

TRANSCRIPTS_PATH: Path = config.ROOT / "voice_transcripts.jsonl"

# Don't bother summarizing trivial calls
MIN_UTTERANCES = 5
MIN_DURATION_S = 20.0

# Cheap + fast — summarization doesn't need the big model
SUMMARY_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso_to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _load_slice(since_ts: float) -> list[dict]:
    """Every JSONL record with ts >= since_ts, sorted oldest->newest."""
    if not TRANSCRIPTS_PATH.exists():
        return []
    out: list[dict] = []
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
                try:
                    ts = float(rec.get("ts") or 0.0)
                except Exception:
                    continue
                if ts < since_ts:
                    continue
                out.append(rec)
    except Exception as e:
        print(f"[call_summary._load_slice] error: {type(e).__name__}: {e}")
        return []
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def _format_transcript(recs: list[dict], max_lines: int = 200) -> str:
    lines: list[str] = []
    for r in recs[-max_lines:]:
        name = (r.get("name") or "someone").strip()
        text = (r.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{name}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def summarize_and_store(since_iso: str) -> dict:
    """
    Summarize every utterance since `since_iso` and drop per-speaker memory
    entries. Returns a report dict with {ok, summary, speakers, utterances,
    duration_s, stored, reason?}. Never raises.
    """
    report: dict = {"ok": False, "stored": 0}

    since_ts = _iso_to_ts(since_iso)
    if not since_ts:
        report["reason"] = f"bad since_iso: {since_iso!r}"
        return report

    recs = _load_slice(since_ts)
    if len(recs) < MIN_UTTERANCES:
        report["reason"] = f"too thin ({len(recs)} utterances)"
        report["utterances"] = len(recs)
        return report

    total_dur = 0.0
    for r in recs:
        try:
            total_dur += float(r.get("dur_s") or 0.0)
        except Exception:
            pass
    if total_dur < MIN_DURATION_S:
        report["reason"] = f"too short ({total_dur:.1f}s)"
        report["utterances"] = len(recs)
        report["duration_s"] = round(total_dur, 1)
        return report

    # Group speakers (skip unknown user_ids — can't attribute mem0 entries)
    speakers: dict[str, dict] = {}
    for r in recs:
        uid = str(r.get("user_id") or "")
        if not uid:
            continue
        nm = (r.get("name") or "").strip()
        if uid not in speakers:
            speakers[uid] = {"name": nm, "count": 0}
        speakers[uid]["count"] += 1
        if nm and not speakers[uid]["name"]:
            speakers[uid]["name"] = nm

    if not speakers:
        report["reason"] = "no attributable speakers"
        report["utterances"] = len(recs)
        return report

    transcript = _format_transcript(recs)
    if not transcript:
        report["reason"] = "empty transcript"
        return report

    # Ask Claude Haiku for a tight summary
    system = (
        "You are summarizing a voice call that just ended in the TNC Nexus Discord. "
        "Write a terse, specific recap of what was actually said — topics, decisions, "
        "questions raised, moments worth remembering.\n\n"
        "Rules:\n"
        "- 3-6 short sentences, plain prose, no bullets\n"
        "- name people by first name when attributing\n"
        "- lowercase, direct, zero fluff, no 'the call was about...'\n"
        "- if people joked around / vibed, note it in one line\n"
        "- never invent anything not in the transcript\n"
        "- if the transcript is chaotic or thin, say so plainly\n"
    )

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": transcript}],
        )
        summary = "".join(
            b.text for b in resp.content if hasattr(b, "text")
        ).strip()
    except Exception as e:
        report["reason"] = f"claude error: {type(e).__name__}: {e}"
        return report

    if not summary:
        report["reason"] = "empty summary"
        return report

    # Build label + write per-speaker mem0 entries
    started_at = datetime.fromtimestamp(since_ts)
    label = started_at.strftime("voice call %Y-%m-%d %H:%M")

    stored = 0
    try:
        import nexus_brain
        m = nexus_brain._get_mem0()
        for uid, info in speakers.items():
            nm = info["name"] or "someone"
            content = f"{label} — {summary}"
            try:
                with nexus_brain._MEM0_LOCK:
                    m.add(
                        messages=[{"role": "user", "content": content}],
                        user_id=uid,
                        agent_id="nexus",
                        metadata={
                            "user_name": nm,
                            "channel": "voice",
                            "scope": "tnc",
                            "tag": "call_summary",
                            "call_started_at": since_iso,
                            "utterance_count": info["count"],
                        },
                    )
                stored += 1
            except Exception as e:
                print(
                    f"[call_summary] m.add failed for {nm} ({uid}): "
                    f"{type(e).__name__}: {e}"
                )
    except Exception as e:
        report["reason"] = f"mem0 init failed: {type(e).__name__}: {e}"
        report["summary"] = summary
        return report

    report.update({
        "ok": stored > 0,
        "summary": summary,
        "speakers": [info["name"] for info in speakers.values()],
        "utterances": len(recs),
        "duration_s": round(total_dur, 1),
        "stored": stored,
        "label": label,
    })
    if stored == 0:
        report["reason"] = "all mem0 writes failed"
    return report
