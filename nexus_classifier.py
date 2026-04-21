"""
Nexus memory classifier.

Classifies a substantive message into:
  - scope: personal | tnc | public    (trust / visibility tier)
  - tag:   gaming | build | life | other    (topic bucket)

Cheap Haiku call per write. Fails closed to personal/other (safest default).

Public contract:
    classify(text: str) -> dict {"scope": str, "tag": str}
"""

from __future__ import annotations

import json
import os
from typing import Optional

from anthropic import Anthropic

import config

SCOPES = ("personal", "tnc", "public")
TAGS = ("gaming", "build", "life", "other")

_CLASSIFIER_MODEL = getattr(config, "CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


_SYSTEM = """You tag memories for a Discord server called The Nexus Collective (TNC).

Return JSON ONLY, no prose:
{"scope": "<personal|tnc|public>", "tag": "<gaming|build|life|other>"}

SCOPE rules (who should see this memory):
  personal = private to the speaker. Feelings, health, family, relationships,
             mental state, anything vulnerable, anything they'd be embarrassed
             if repeated. DEFAULT TO personal WHEN UNSURE.
  tnc      = relevant to the whole TNC group: build decisions, group plans,
             server-wide jokes, shared projects, things they'd repeat at the
             next meetup.
  public   = broadly true, not sensitive, no group context needed.
             Examples: "Elden Ring Nightreign came out 2025".

TAG rules (what the memory is about):
  gaming = games, FPS, Tarkov, playtime, lobby matchmaking
  build  = code, AI, Nexus itself, TNC infra, projects, shipping
  life   = health, food, sleep, family, relationships, mood, jobs
  other  = none of the above

If a message mixes two, pick the dominant one.
Output MUST be valid JSON with exactly those two keys. No explanation."""


def classify(text: str) -> dict:
    """Return {'scope': str, 'tag': str}. Fails to {personal, other}."""
    fallback = {"scope": "personal", "tag": "other"}
    text = (text or "").strip()
    if not text:
        return fallback
    try:
        resp = _get_client().messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=64,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text[:1000]}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip("` \n")
        data = json.loads(raw)
        scope = str(data.get("scope", "")).lower()
        tag = str(data.get("tag", "")).lower()
        if scope not in SCOPES:
            scope = "personal"
        if tag not in TAGS:
            tag = "other"
        return {"scope": scope, "tag": tag}
    except Exception as e:
        print(f"[nexus_classifier] fallback after error: {type(e).__name__}: {e}")
        return fallback


if __name__ == "__main__":
    # Quick smoke test
    samples = [
        "i just shipped the dave decrypt fix for the bot",
        "my mom's surgery is next week and i'm scared",
        "tarkov wipes on friday, anyone down for a scav run",
        "elden ring nightreign dropped in 2025",
        "we should add cross-user memory to nexus this week",
    ]
    for s in samples:
        print(f"{s}\n  -> {classify(s)}\n")
