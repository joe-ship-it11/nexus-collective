"""
Nexus video brain — youtube link in, summary + classification out.

Public API:
    extract_video_id(url) -> str | None
    fetch_transcript(video_id) -> dict {ok, text, lang, source, reason?}
    analyze(url) -> dict {
        ok, video_id, url, title, summary,
        scope, tag, substantive,
        char_count, transcript_lang,
        reason?  # only set if ok=False
    }

Pipeline:
    1. Parse the URL into a video id (handles youtu.be, watch?v=, shorts).
    2. Pull captions via youtube-transcript-api (free, no key).
    3. Cap text at MAX_TRANSCRIPT_CHARS (long videos truncated, head+tail).
    4. Claude Haiku → terse 3-6 sentence recap in lowercase nexus voice.
    5. nexus_classifier → scope + tag.
    6. "substantive" = scope in {tnc, public} AND tag != other.

Failure modes (all return ok=False with a reason, never raise):
    - bad/unsupported URL
    - no captions on the video (member-only, age-restricted, no transcript)
    - youtube-transcript-api raises (NoTranscriptFound, TranscriptsDisabled, etc.)
    - Claude API error
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from anthropic import Anthropic

import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
MAX_TRANSCRIPT_CHARS = 12_000   # ~3k tokens. Head + tail if longer.
SUMMARY_MAX_TOKENS = 500

PREFERRED_LANGS = ("en", "en-US", "en-GB", "a.en")  # last is auto-generated

# Whisper fallback caps — protects from runaway videos + event loop from starving
# CPU int8 small runs ~2-3x realtime. 1500s (25min) video = ~8-12min transcribe.
# Higher than that and we fight voice-listen for CPU + risk interaction token death
# even with the channel.send pattern.
MAX_VIDEO_DURATION_S = 1500
WHISPER_BEAM_SIZE = 1           # speed > accuracy for /watch use-case

# Serialize whisper transcribe calls across voice-listen + /watch so they don't
# contend for the same model instance (faster-whisper is safe concurrently but
# two heavy calls at once starve CPU and kill the gateway heartbeat).
_WHISPER_LOCK = threading.Lock()

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_anthropic_client: Optional[Anthropic] = None


def _client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
def extract_video_id(url: str) -> Optional[str]:
    """Pull an 11-char YouTube id out of a URL. None if not a yt URL."""
    if not url:
        return None
    url = url.strip()

    # Bare 11-char id
    if _VIDEO_ID_RE.match(url):
        return url

    try:
        u = urlparse(url)
    except Exception:
        return None

    host = (u.hostname or "").lower().lstrip("www.")
    path = u.path or ""

    # youtu.be/<id>
    if host == "youtu.be":
        candidate = path.lstrip("/").split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # youtube.com / m.youtube.com / music.youtube.com
    if host.endswith("youtube.com"):
        # /watch?v=<id>
        if path == "/watch":
            qs = parse_qs(u.query or "")
            v = (qs.get("v") or [""])[0]
            return v if _VIDEO_ID_RE.match(v) else None

        # /shorts/<id> , /embed/<id> , /v/<id> , /live/<id>
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if path.startswith(prefix):
                candidate = path[len(prefix):].split("/", 1)[0].split("?", 1)[0]
                return candidate if _VIDEO_ID_RE.match(candidate) else None

    return None


# ---------------------------------------------------------------------------
# Transcript fetch
# ---------------------------------------------------------------------------
def fetch_transcript(video_id: str, allow_whisper_fallback: bool = True) -> dict:
    """
    Return {ok, text, lang, source, reason?}. Never raises.

    Tries youtube captions first. If that fails for a "no captions" reason
    (TranscriptsDisabled etc.) AND allow_whisper_fallback=True, downloads
    audio via yt-dlp and transcribes via faster-whisper.
    """
    out: dict = {"ok": False, "text": "", "lang": "", "source": "youtube-captions"}
    if not _VIDEO_ID_RE.match(video_id or ""):
        out["reason"] = "bad video_id"
        return out

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        out["reason"] = "youtube-transcript-api not installed"
        return out

    # Friendly mapping for known exception types from the library
    def _friendly(exc: Exception) -> str:
        name = type(exc).__name__
        msg_map = {
            "TranscriptsDisabled": "subtitles are disabled on this video",
            "NoTranscriptFound": "no transcript in a language i can read",
            "VideoUnavailable": "video is unavailable (private/removed/region-locked)",
            "AgeRestricted": "video is age-restricted — yt won't give me captions",
            "TooManyRequests": "youtube rate-limited me — try again in a bit",
            "NotTranslatable": "transcript exists but can't be translated to english",
            "TranslationLanguageNotAvailable": "no english translation available",
            "FailedToCreateConsentCookie": "yt cookie consent dance failed",
        }
        return msg_map.get(name, f"{name}: {exc}")

    # Try new (>=1.0) instance API first, then fall back to legacy class-method.
    new_api_err: Optional[Exception] = None

    if hasattr(YouTubeTranscriptApi, "list") or hasattr(YouTubeTranscriptApi, "list_transcripts"):
        try:
            api = YouTubeTranscriptApi()
            tlist = api.list(video_id) if hasattr(api, "list") else api.list_transcripts(video_id)

            # Prefer manual english, then auto english, then anything translatable
            t = None
            try:
                t = tlist.find_transcript(list(PREFERRED_LANGS))
            except Exception:
                pass
            if t is None:
                try:
                    t = tlist.find_generated_transcript(["en"])
                except Exception:
                    pass
            if t is None:
                for tr in tlist:
                    try:
                        t = tr.translate("en") if getattr(tr, "is_translatable", False) else tr
                        break
                    except Exception:
                        t = tr
                        break

            if t is None:
                out["reason"] = "no transcripts available"
                return out

            fetched = t.fetch()
            parts: list[str] = []
            for seg in fetched:
                if isinstance(seg, dict):
                    txt = (seg.get("text") or "").strip()
                else:
                    txt = (getattr(seg, "text", "") or "").strip()
                if txt and txt != "[Music]":
                    parts.append(txt)
            text = " ".join(parts).strip()
            if not text:
                out["reason"] = "transcript empty"
                return out
            out.update({
                "ok": True,
                "text": text,
                "lang": getattr(t, "language_code", "") or getattr(t, "language", ""),
            })
            return out
        except Exception as e:
            new_api_err = e
            # Known terminal caption errors → skip legacy retry, go to fallback.
            terminal_caption_errs = (
                "TranscriptsDisabled", "NoTranscriptFound",
                "NotTranslatable", "TranslationLanguageNotAvailable",
                "VideoUnavailable", "AgeRestricted", "TooManyRequests",
            )
            if type(e).__name__ not in terminal_caption_errs:
                # Unknown error — try legacy class-method API
                if hasattr(YouTubeTranscriptApi, "get_transcript"):
                    try:
                        segs = YouTubeTranscriptApi.get_transcript(
                            video_id, languages=list(PREFERRED_LANGS),
                        )
                        text = " ".join(
                            (s.get("text") or "").strip() for s in segs
                            if (s.get("text") or "").strip() and s.get("text") != "[Music]"
                        ).strip()
                        if text:
                            out.update({"ok": True, "text": text, "lang": "en"})
                            return out
                        new_api_err = RuntimeError("legacy api returned empty")
                    except Exception as e2:
                        new_api_err = e2

    # Neither API path worked
    if new_api_err is not None:
        captions_reason = _friendly(new_api_err)
    else:
        captions_reason = "youtube-transcript-api has no usable entrypoint"

    # Whisper fallback — only for "no captions available" failures, not
    # "video unavailable" / "rate limited" / "age restricted" (those won't
    # let yt-dlp pull audio either, no point retrying).
    fallback_eligible = bool(new_api_err) and type(new_api_err).__name__ in (
        "TranscriptsDisabled", "NoTranscriptFound",
        "NotTranslatable", "TranslationLanguageNotAvailable",
    )
    if allow_whisper_fallback and fallback_eligible:
        wh = _fetch_via_whisper(video_id)
        if wh.get("ok"):
            return wh
        # Both failed — surface the whisper reason since user asked for fallback
        out["reason"] = f"captions: {captions_reason}; whisper: {wh.get('reason')}"
        return out

    out["reason"] = captions_reason
    return out


# ---------------------------------------------------------------------------
# Whisper fallback — yt-dlp downloads audio, faster-whisper transcribes
# ---------------------------------------------------------------------------
def _fetch_via_whisper(video_id: str) -> dict:
    """
    Download audio with yt-dlp, transcribe with faster-whisper. Returns
    {ok, text, lang, source, duration_s, reason?}. Never raises.

    Reuses the whisper model that nexus_listen has loaded (no double-load).
    """
    out: dict = {"ok": False, "text": "", "lang": "", "source": "whisper"}

    # 1. yt-dlp available?
    try:
        import yt_dlp
    except ImportError:
        out["reason"] = "yt-dlp not installed"
        return out

    # 2. Probe duration first (cheap) to enforce cap
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False,
            )
            duration_s = float(info.get("duration") or 0)
            title = info.get("title") or ""
    except Exception as e:
        out["reason"] = f"yt-dlp probe failed: {type(e).__name__}: {e}"
        return out

    if duration_s and duration_s > MAX_VIDEO_DURATION_S:
        mins = int(duration_s // 60)
        out["reason"] = f"video is {mins}min — cap is {MAX_VIDEO_DURATION_S//60}min for whisper fallback"
        return out

    # 3. Download bestaudio to a fresh temp dir
    tmpdir = Path(tempfile.mkdtemp(prefix="nexus_video_"))
    audio_path: Optional[Path] = None
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(tmpdir / "audio.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:
            out["reason"] = f"yt-dlp download failed: {type(e).__name__}: {e}"
            return out

        # Find what got written (extension is whatever yt-dlp picked)
        candidates = sorted(tmpdir.glob("audio.*"))
        if not candidates:
            out["reason"] = "yt-dlp wrote no audio file"
            return out
        audio_path = candidates[0]

        # 4. Transcribe via reused faster-whisper model
        try:
            import nexus_listen
            model = nexus_listen._get_whisper()
        except Exception as e:
            out["reason"] = f"whisper model load failed: {type(e).__name__}: {e}"
            return out

        # Serialize so we don't fight the voice-listen whisper for CPU
        try:
            with _WHISPER_LOCK:
                segments, info = model.transcribe(
                    str(audio_path),
                    beam_size=WHISPER_BEAM_SIZE,
                    vad_filter=True,           # drop silence — videos have lots
                    vad_parameters={"min_silence_duration_ms": 500},
                    # No language= so whisper auto-detects (works for non-en uploads)
                )
                parts: list[str] = []
                for s in segments:
                    t = (s.text or "").strip()
                    if t:
                        parts.append(t)
                text = " ".join(parts).strip()
        except Exception as e:
            out["reason"] = f"whisper transcribe failed: {type(e).__name__}: {e}"
            return out

        if not text:
            out["reason"] = "whisper produced empty transcript (silent / music-only?)"
            return out

        out.update({
            "ok": True,
            "text": text,
            "lang": getattr(info, "language", "") or "",
            "duration_s": round(duration_s, 1) if duration_s else 0,
            "title": title,
        })
        return out

    finally:
        # Cleanup the temp dir no matter what
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------
_SUMMARY_SYSTEM = """you summarize youtube videos for a discord called The Nexus Collective.

write a tight, specific recap of what the video actually covered.

rules:
- 3-6 short sentences, plain prose, no bullets, no headers
- lowercase, direct, zero fluff
- never start with "this video is about" or "the speaker discusses"
- name specific things (people, claims, examples) when present in the transcript
- if the video is ranty / vibey / shitposty, say so plainly
- if the transcript is garbled or thin, say so
- never invent anything not in the transcript"""


def _trim_transcript(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """If too long, take first 70% + last 30% with a [...] marker."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - 8
    return f"{text[:head]} [...] {text[-tail:]}"


def summarize(transcript: str, title: Optional[str] = None) -> dict:
    """Return {ok, summary, reason?}. Never raises."""
    out: dict = {"ok": False, "summary": ""}
    if not transcript or not transcript.strip():
        out["reason"] = "empty transcript"
        return out

    body = _trim_transcript(transcript)
    user_msg = body if not title else f"video title: {title}\n\ntranscript:\n{body}"

    try:
        resp = _client().messages.create(
            model=SUMMARY_MODEL,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            b.text for b in resp.content if hasattr(b, "text")
        ).strip()
        if not text:
            out["reason"] = "claude returned empty"
            return out
        out.update({"ok": True, "summary": text})
        return out
    except Exception as e:
        out["reason"] = f"claude error: {type(e).__name__}: {e}"
        return out


# ---------------------------------------------------------------------------
# Public: analyze
# ---------------------------------------------------------------------------
def analyze(url: str, title: Optional[str] = None) -> dict:
    """
    Full pipeline: URL -> {ok, video_id, url, title, summary, scope, tag,
    substantive, char_count, transcript_lang, reason?}. Never raises.
    """
    report: dict = {"ok": False, "url": url, "title": title}

    vid = extract_video_id(url)
    if not vid:
        report["reason"] = "not a recognizable youtube url"
        return report
    report["video_id"] = vid

    tx = fetch_transcript(vid)
    if not tx.get("ok"):
        report["reason"] = tx.get("reason") or "transcript fetch failed"
        return report
    report["char_count"] = len(tx["text"])
    report["transcript_lang"] = tx.get("lang", "")
    report["transcript_source"] = tx.get("source", "youtube-captions")
    # Whisper fallback may discover the title on its own
    if not title and tx.get("title"):
        report["title"] = tx["title"]
        title = tx["title"]

    summary_res = summarize(tx["text"], title=title)
    if not summary_res.get("ok"):
        report["reason"] = summary_res.get("reason") or "summary failed"
        return report
    summary = summary_res["summary"]
    report["summary"] = summary

    # Classify the SUMMARY (cheaper, captures the gist already)
    try:
        import nexus_classifier
        klass = nexus_classifier.classify(summary)
        scope = klass.get("scope", "personal")
        tag = klass.get("tag", "other")
    except Exception as e:
        scope, tag = "personal", "other"
        report["classifier_error"] = f"{type(e).__name__}: {e}"

    report["scope"] = scope
    report["tag"] = tag
    # Substantive = worth offering "save" button. Personal videos / "other" tag = ephemeral.
    report["substantive"] = scope in ("tnc", "public") and tag != "other"
    report["ok"] = True
    return report


if __name__ == "__main__":
    # smoke test
    import sys
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://youtu.be/dQw4w9WgXcQ"
    print(f"\n[test] {test_url}")
    print(f"[test] video_id = {extract_video_id(test_url)}")
    r = analyze(test_url)
    if r.get("ok"):
        print(f"\nscope={r['scope']} tag={r['tag']} substantive={r['substantive']}")
        print(f"chars={r['char_count']} lang={r['transcript_lang']}\n")
        print(r["summary"])
    else:
        print(f"\nFAILED: {r.get('reason')}")
