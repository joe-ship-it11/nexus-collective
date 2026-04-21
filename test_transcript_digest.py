"""
Tests for transcript_digest. Pure stdlib + pytest, no network, no LLM.

Each test monkeypatches transcript_digest.TRANSCRIPTS_PATH to a tmp_path
file so the real voice_transcripts.jsonl is never touched.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

import transcript_digest as td


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_jsonl(p: Path, records: list[dict]) -> None:
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(ts: float, user_id: str, name: str, text: str,
         dur_s: float = 1.0, triggered: bool = False) -> dict:
    return {
        "ts": ts,
        "iso": datetime.fromtimestamp(ts).isoformat(),
        "user_id": user_id,
        "name": name,
        "text": text,
        "dur_s": dur_s,
        "triggered": triggered,
    }


@pytest.fixture
def tpath(tmp_path, monkeypatch):
    """Point the module at an isolated JSONL file."""
    p = tmp_path / "voice_transcripts.jsonl"
    monkeypatch.setattr(td, "TRANSCRIPTS_PATH", p)
    return p


# ---------------------------------------------------------------------------
# Missing / empty file behavior
# ---------------------------------------------------------------------------
def test_missing_file_returns_empty(tpath):
    # file does not exist
    assert not tpath.exists()
    assert td.get_recent_window() == []
    assert td.top_topics() == []
    assert td.format_for_prompt() == ""
    # user summary + today digest return empty-shaped dicts
    s = td.get_user_summary("123")
    assert s["utterances"] == 0
    assert s["samples"] == []
    d = td.get_today_digest()
    assert d["utterances"] == 0
    assert d["unique_speakers"] == 0
    assert d["top_speakers"] == []
    assert d["longest_utterance"] is None


def test_empty_file_returns_empty(tpath):
    tpath.write_text("", encoding="utf-8")
    assert td.get_recent_window() == []
    assert td.top_topics() == []
    assert td.format_for_prompt() == ""
    assert td.get_today_digest()["utterances"] == 0
    assert td.get_user_summary("x")["utterances"] == 0


def test_malformed_lines_are_skipped(tpath):
    now = time.time()
    lines = [
        "not-json",
        json.dumps(_rec(now - 10, "u1", "alice", "hello there")),
        "{not: valid",
        json.dumps(_rec(now - 5, "u1", "alice", "second line")),
    ]
    tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    recs = td.get_recent_window(seconds=60)
    assert len(recs) == 2
    assert [r["text"] for r in recs] == ["hello there", "second line"]


# ---------------------------------------------------------------------------
# get_recent_window
# ---------------------------------------------------------------------------
def test_recent_window_filters_by_seconds(tpath):
    now = time.time()
    _write_jsonl(tpath, [
        _rec(now - 600, "u1", "alice", "way back"),
        _rec(now - 90, "u1", "alice", "recent-ish"),
        _rec(now - 10, "u2", "bob", "just now"),
    ])
    recent = td.get_recent_window(seconds=120)
    texts = [r["text"] for r in recent]
    assert "way back" not in texts
    assert "recent-ish" in texts
    assert "just now" in texts
    # chronological order
    assert recent[0]["text"] == "recent-ish"
    assert recent[-1]["text"] == "just now"


def test_recent_window_zero_or_negative(tpath):
    now = time.time()
    _write_jsonl(tpath, [_rec(now, "u", "n", "hi")])
    assert td.get_recent_window(seconds=0) == []
    assert td.get_recent_window(seconds=-5) == []


# ---------------------------------------------------------------------------
# get_user_summary
# ---------------------------------------------------------------------------
def test_user_summary_aggregates(tpath):
    now = time.time()
    _write_jsonl(tpath, [
        _rec(now - 100, "u1", "alice", "first thing", dur_s=2.0, triggered=False),
        _rec(now - 50, "u1", "alice", "hey nexus wake up", dur_s=3.0, triggered=True),
        _rec(now - 40, "u2", "bob", "not alice", dur_s=1.0),
        _rec(now - 10, "u1", "alice", "third alice", dur_s=1.0, triggered=False),
    ])
    s = td.get_user_summary("u1")
    assert s["user_id"] == "u1"
    assert s["name"] == "alice"
    assert s["utterances"] == 3
    assert s["trigger_count"] == 1
    assert s["total_duration_s"] == pytest.approx(6.0)
    assert s["avg_duration_s"] == pytest.approx(2.0)
    assert s["samples"][0] == "first thing"
    assert s["samples"][-1] == "third alice"


def test_user_summary_unknown_user(tpath):
    now = time.time()
    _write_jsonl(tpath, [_rec(now, "u1", "alice", "hi")])
    s = td.get_user_summary("does-not-exist")
    assert s["utterances"] == 0
    assert s["samples"] == []


def test_user_summary_limit_applied(tpath):
    now = time.time()
    recs = [_rec(now - (100 - i), "u1", "alice", f"msg {i}") for i in range(20)]
    _write_jsonl(tpath, recs)
    s = td.get_user_summary("u1", limit=5)
    assert s["utterances"] == 5
    # the 5 most recent should be msgs 15..19
    assert s["samples"][-1] == "msg 19"


# ---------------------------------------------------------------------------
# get_today_digest
# ---------------------------------------------------------------------------
def test_today_digest_counts_today_only(tpath):
    now = time.time()
    # one record from yesterday, three from today
    yesterday = now - 26 * 3600
    _write_jsonl(tpath, [
        _rec(yesterday, "u1", "alice", "old", dur_s=1.0),
        _rec(now - 300, "u1", "alice", "today one", dur_s=2.5, triggered=True),
        _rec(now - 200, "u2", "bob", "today two", dur_s=5.0),
        _rec(now - 100, "u1", "alice", "today three", dur_s=1.0),
    ])
    d = td.get_today_digest()
    assert d["date"] == datetime.now().strftime("%Y-%m-%d")
    assert d["utterances"] == 3
    assert d["unique_speakers"] == 2
    assert d["trigger_count"] == 1
    speakers = dict(d["top_speakers"])
    assert speakers.get("alice") == 2
    assert speakers.get("bob") == 1
    assert d["longest_utterance"]["name"] == "bob"
    assert d["longest_utterance"]["text"] == "today two"
    assert d["longest_utterance"]["dur_s"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# top_topics
# ---------------------------------------------------------------------------
def test_top_topics_counts_and_stoplist(tpath):
    now = time.time()
    _write_jsonl(tpath, [
        _rec(now - 60, "u1", "alice", "the project synthesizer is the thing we need"),
        _rec(now - 30, "u2", "bob", "synthesizer and project and synthesizer"),
        _rec(now - 10, "u1", "alice", "I think the project ships tomorrow"),
    ])
    topics = td.top_topics(limit=5)
    # stop words like "the", "is", "we", "and", "i", "think" must be dropped
    assert "the" not in topics
    assert "and" not in topics
    assert "is" not in topics
    # "synthesizer" (3x) should outrank "project" (3x)/tied; both must appear
    assert "synthesizer" in topics
    assert "project" in topics
    # first element = most frequent
    assert topics[0] in {"synthesizer", "project"}


def test_top_topics_ignores_old_records(tpath):
    now = time.time()
    old = now - 25 * 3600
    _write_jsonl(tpath, [
        _rec(old, "u1", "alice", "ancient synthesizer talk"),
        _rec(now - 60, "u2", "bob", "fresh launchpad launchpad keyboard"),
    ])
    topics = td.top_topics(limit=5)
    assert "launchpad" in topics
    assert "synthesizer" not in topics


def test_top_topics_empty_limit(tpath):
    now = time.time()
    _write_jsonl(tpath, [_rec(now, "u", "n", "word word word")])
    assert td.top_topics(limit=0) == []


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------
def test_format_for_prompt_missing_file_empty(tpath):
    assert td.format_for_prompt() == ""


def test_format_for_prompt_outputs_name_text_lines(tpath):
    now = time.time()
    _write_jsonl(tpath, [
        _rec(now - 600, "u1", "alice", "too old"),
        _rec(now - 60, "u1", "alice", "hello  there"),
        _rec(now - 30, "u2", "bob", "second line"),
    ])
    out = td.format_for_prompt(seconds=120, max_lines=20)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == "alice: hello there"  # whitespace collapsed
    assert lines[1] == "bob: second line"


def test_format_for_prompt_respects_max_lines(tpath):
    now = time.time()
    recs = [_rec(now - (100 - i), "u1", "alice", f"line {i}") for i in range(10)]
    _write_jsonl(tpath, recs)
    out = td.format_for_prompt(seconds=200, max_lines=3)
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[-1] == "alice: line 9"
