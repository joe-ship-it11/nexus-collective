"""
Nexus listen: Discord VC audio capture + Whisper transcription + trigger detection.

Pipeline:
  VC audio (48kHz stereo PCM16)  → NexusAudioSink buffers per user
  silence > SILENCE_MS on a user  → flush buffer → whisper transcribe
  transcript contains "nexus"     → callback triggers reply pipeline

Whisper model is loaded lazily once per process. Default: small/int8 on CPU.
Swap config.WHISPER_MODEL / WHISPER_DEVICE to use GPU later.
"""

import asyncio
import os
import re
import tempfile
import threading
import time
import uuid
import wave
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

import discord
from discord.ext import voice_recv

import config


# ---------------------------------------------------------------------------
# Monkey-patch: voice_recv 0.5.2a crashes its packet-router thread on the
# first OpusError ("corrupted stream"). With DAVE (discord E2EE) active,
# some packets look corrupted to the opus decoder. We swallow those errors
# so one bad packet doesn't kill the whole listen session.
# ---------------------------------------------------------------------------
def _install_voice_recv_patch():
    try:
        from discord.ext.voice_recv import opus as _vropus
        from discord.opus import OpusError

        _orig_decode = _vropus.PacketDecoder._decode_packet
        _orig_process = _vropus.PacketDecoder._process_packet

        if getattr(_vropus.PacketDecoder, "_nexus_patched", False):
            return

        _stats = {"ok": 0, "opus_err": 0, "other_err": 0, "last_log": 0.0}

        def _safe_decode(self, packet):
            try:
                result = _orig_decode(self, packet)
                _stats["ok"] += 1
                return result
            except OpusError as e:
                _stats["opus_err"] += 1
                now = time.time()
                if now - _stats["last_log"] > 5.0:
                    print(f"[nexus_listen.patch] decode stats: "
                          f"ok={_stats['ok']} opus_err={_stats['opus_err']} "
                          f"other={_stats['other_err']}", flush=True)
                    _stats["last_log"] = now
                # Corrupted / DAVE-encrypted / unexpected payload — skip this packet.
                return packet, b""
            except Exception as e:
                _stats["other_err"] += 1
                print(f"[nexus_listen.patch] decode error: {type(e).__name__}: {e}",
                      flush=True)
                return packet, b""

        def _safe_process(self, packet):
            try:
                return _orig_process(self, packet)
            except OpusError:
                return None
            except Exception as e:
                print(f"[nexus_listen.patch] process error: {type(e).__name__}: {e}",
                      flush=True)
                return None

        _vropus.PacketDecoder._decode_packet = _safe_decode
        _vropus.PacketDecoder._process_packet = _safe_process
        _vropus.PacketDecoder._nexus_patched = True
        print("[nexus_listen.patch] voice_recv PacketDecoder patched "
              "(OpusError swallowed)", flush=True)
    except Exception as e:
        print(f"[nexus_listen.patch] failed to install patch: "
              f"{type(e).__name__}: {e}", flush=True)


_install_voice_recv_patch()


# ---------------------------------------------------------------------------
# Monkey-patch: voice_recv 0.5.2a has ZERO DAVE/MLS support. On DAVE-enabled
# servers (Discord default since 2024), the bytes arriving at the opus decoder
# are still E2EE-encrypted → ~50% OpusError + ~50% random-noise PCM.
#
# Fix: wrap PacketDecoder._decode_packet. Before feeding bytes to opus,
# call davey.DaveSession.decrypt(user_id, MediaType.audio, packet.decrypted_data)
# using the session discord.py already maintains at voice_client._connection.dave_session.
# When the session is ready and the user isn't in passthrough mode, this
# produces the actual Opus payload that opus can decode to real PCM.
#
# Dead-end paths tried first (documented in reference_voice_recv_no_dave.md):
#   - Discord UI DAVE toggle → doesn't exist for users.
#   - Monkey-patch max_dave_protocol_version=0 → server rejects with 4017.
# ---------------------------------------------------------------------------
def _install_dave_decrypt_patch():
    try:
        import davey
        from discord.ext.voice_recv import opus as _vropus

        if getattr(_vropus.PacketDecoder, "_nexus_dave_patched", False):
            return

        # The previously-installed safe-decode wrapper is what's currently on
        # PacketDecoder._decode_packet. We wrap *that* so we keep OpusError
        # swallowing in the inner layer.
        _inner_decode = _vropus.PacketDecoder._decode_packet

        _dave_stats = {
            "decrypted": 0,
            "passthrough": 0,
            "no_session": 0,
            "not_ready": 0,
            "no_uid": 0,
            "decrypt_err": 0,
            "last_log": 0.0,
        }

        def _log_stats():
            now = time.time()
            if now - _dave_stats["last_log"] > 10.0:
                print(f"[nexus_listen.dave] decrypted={_dave_stats['decrypted']} "
                      f"passthrough={_dave_stats['passthrough']} "
                      f"no_session={_dave_stats['no_session']} "
                      f"not_ready={_dave_stats['not_ready']} "
                      f"no_uid={_dave_stats['no_uid']} "
                      f"decrypt_err={_dave_stats['decrypt_err']}", flush=True)
                _dave_stats["last_log"] = now

        def _dave_aware_decode(self, packet):
            if packet is None:
                return _inner_decode(self, packet)
            try:
                vc = self.sink.voice_client
                state = getattr(vc, "_connection", None) or getattr(vc, "_state", None)
                session = getattr(state, "dave_session", None) if state else None

                if session is None:
                    _dave_stats["no_session"] += 1
                    _log_stats()
                    return _inner_decode(self, packet)

                if not getattr(session, "ready", False):
                    _dave_stats["not_ready"] += 1
                    _log_stats()
                    return _inner_decode(self, packet)

                uid = self._cached_id
                if uid is None:
                    try:
                        uid = vc._get_id_from_ssrc(self.ssrc)
                    except Exception:
                        uid = None
                if uid is None:
                    _dave_stats["no_uid"] += 1
                    _log_stats()
                    return _inner_decode(self, packet)

                # Passthrough users' packets are already unencrypted — feed
                # them straight to opus without DAVE decrypt.
                try:
                    if session.can_passthrough(uid):
                        _dave_stats["passthrough"] += 1
                        _log_stats()
                        return _inner_decode(self, packet)
                except Exception:
                    pass

                # DAVE-decrypt in place, keeping original bytes so we can restore
                # (defensive — don't assume packet is disposable).
                orig = packet.decrypted_data
                try:
                    decrypted = session.decrypt(
                        uid, davey.MediaType.audio, orig
                    )
                except Exception as e:
                    _dave_stats["decrypt_err"] += 1
                    _log_stats()
                    # Fall back to inner decode with original bytes — may OpusError,
                    # which the inner patch will swallow.
                    return _inner_decode(self, packet)

                packet.decrypted_data = decrypted
                try:
                    result = _inner_decode(self, packet)
                    _dave_stats["decrypted"] += 1
                    _log_stats()
                    return result
                finally:
                    packet.decrypted_data = orig
            except Exception as e:
                print(f"[nexus_listen.dave] outer error: "
                      f"{type(e).__name__}: {e}", flush=True)
                return _inner_decode(self, packet)

        _vropus.PacketDecoder._decode_packet = _dave_aware_decode
        _vropus.PacketDecoder._nexus_dave_patched = True
        print("[nexus_listen.dave] PacketDecoder DAVE-aware decode patched "
              f"(davey v{getattr(davey, '__version__', '?')}, "
              f"protocol v{getattr(davey, 'DAVE_PROTOCOL_VERSION', '?')})",
              flush=True)
    except Exception as e:
        print(f"[nexus_listen.dave] failed to install DAVE patch: "
              f"{type(e).__name__}: {e}", flush=True)


_install_dave_decrypt_patch()


# ---------------------------------------------------------------------------
# Whisper — lazy singleton
# ---------------------------------------------------------------------------
_whisper_model = None
_whisper_lock = threading.Lock()

WHISPER_MODEL_SIZE = getattr(config, "WHISPER_MODEL", "small")
WHISPER_DEVICE = getattr(config, "WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = getattr(config, "WHISPER_COMPUTE_TYPE", "int8")


def _get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            print(f"[nexus_listen] loading whisper {WHISPER_MODEL_SIZE} "
                  f"on {WHISPER_DEVICE}/{WHISPER_COMPUTE}…", flush=True)
            _whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE,
            )
            print(f"[nexus_listen] whisper ready", flush=True)
    return _whisper_model


def transcribe_wav(path: str) -> str:
    """Run Whisper on a WAV file, return concatenated text."""
    model = _get_whisper()
    try:
        segments, info = model.transcribe(
            path,
            language="en",
            beam_size=1,               # speed over accuracy for MVP
            vad_filter=False,          # don't drop our short utterances
        )
        seg_list = list(segments)
        text = " ".join(s.text.strip() for s in seg_list).strip()
        if not text:
            print(f"[nexus_listen.transcribe] empty result: "
                  f"lang={getattr(info, 'language', '?')} "
                  f"prob={getattr(info, 'language_probability', 0):.2f} "
                  f"dur={getattr(info, 'duration', 0):.2f}s "
                  f"segs={len(seg_list)}", flush=True)
        return text
    except Exception as e:
        print(f"[nexus_listen.transcribe] error: {type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Audio sink — per-user PCM buffering + silence-based flush
# ---------------------------------------------------------------------------
TEMP_DIR = Path(__file__).parent / "listen_temp"
TEMP_DIR.mkdir(exist_ok=True)

# Silence gap (seconds) that triggers a buffer flush
SILENCE_SECONDS = getattr(config, "LISTEN_SILENCE_SECONDS", 1.2)
# Minimum buffer length (seconds of audio) to bother transcribing
MIN_BUFFER_SECONDS = getattr(config, "LISTEN_MIN_BUFFER_SECONDS", 0.6)
# Cooldown after a triggered reply (seconds) — ignore that user's audio briefly
COOLDOWN_SECONDS = getattr(config, "LISTEN_COOLDOWN_SECONDS", 4.0)

# Discord voice: 48kHz, 16-bit PCM, stereo → 192 bytes per ms
BYTES_PER_SECOND = 48000 * 2 * 2  # 192000
PCM_FRAME_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPWIDTH = 2

# Word-boundary "nexus" matcher — mirrors the text trigger in nexus_bot.py
_NAME_TRIGGER = re.compile(r"\bnexus\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# NAMO — Nexus Ambient Mode Opt-in
#
# When enabled via set_interject_mode(True), Nexus can occasionally interject
# during voice conversations WITHOUT a wake-word, gated by pause detection,
# utterance quality, rate limits, and name-trigger recency. Default OFF.
# When OFF, this entire code path is inert — no callbacks, no state churn,
# no extra log lines. The \bnexus\b path is NOT modified.
# ---------------------------------------------------------------------------
NAMO_PAUSE_SECONDS = getattr(config, "NAMO_PAUSE_SECONDS", 2.5)
NAMO_COOLDOWN_SECONDS = getattr(config, "NAMO_COOLDOWN_SECONDS", 120.0)
NAMO_MIN_WORDS = 6              # fires only when word_count > NAMO_MIN_WORDS
NAMO_NAME_TRIGGER_HOLDOFF = 10.0  # never fire within 10s of a name-trigger reply

# Signal regex — utterance must contain at least one of these markers to be
# considered a "turn-open" candidate (a question, hedge, or invitation).
_NAMO_SIGNAL = re.compile(
    r"\?|what do you think|should i|i feel|we should|wouldn't it|right\?|yknow",
    re.IGNORECASE,
)

# Module-level mode flag — default OFF. Guarded by a lock for thread safety
# (the sink's flusher thread reads this; the main loop writes via the toggle).
_interject_mode: bool = False
_interject_mode_lock = threading.Lock()


def set_interject_mode(on: bool) -> None:
    """Toggle NAMO interjection mode.

    Default OFF. When OFF, no NAMO callback ever fires and no NAMO state is
    accumulated in the sink — behavior is identical to pre-NAMO code.
    When ON, qualifying post-pause utterances can trigger `interject_cb` on
    the sink (if one was provided), subject to rate limits + name-trigger
    holdoff + voice_client.is_playing() checks.
    """
    global _interject_mode
    with _interject_mode_lock:
        _interject_mode = bool(on)
    print(
        f"[nexus_listen.namo] interject_mode="
        f"{'ON' if _interject_mode else 'OFF'}",
        flush=True,
    )


def _is_interject_mode_on() -> bool:
    with _interject_mode_lock:
        return _interject_mode


# ---------------------------------------------------------------------------
# Natural-opening detection (Agent D)
#
# Detects moments in a call where Nexus could plausibly chime in without
# being rude: a substantive utterance ended, then >= VOICE_OPENING_SILENCE_S
# of silence. Handlers registered via register_opening_handler() are invoked
# async on the bot's event loop. This module ONLY emits — it does not decide
# whether Nexus actually speaks; that's the proactive module's job.
# ---------------------------------------------------------------------------
VOICE_OPENING_SILENCE_S = float(os.environ.get("VOICE_OPENING_SILENCE_S", 5.0))
VOICE_OPENING_COOLDOWN_S = float(os.environ.get("VOICE_OPENING_COOLDOWN_S", 90.0))
VOICE_OPENING_MIN_TEXT_LEN = int(os.environ.get("VOICE_OPENING_MIN_TEXT_LEN", 12))

# List of registered async callables: async (voice_channel, recent_lines) -> None
_opening_handlers: list = []

# Per-voice-channel state. Keyed by voice channel id.
_last_substantive_ts: dict[int, float] = {}
_last_substantive_lines: dict[int, list] = defaultdict(list)
_last_opening_fire_ts: dict[int, float] = {}
# Track the last_ts value we already fired an opening for — prevents
# re-firing until a NEW substantive utterance arrives (re-arm).
_fired_for_last_ts: dict[int, float] = {}
# Map ch_id -> voice_client so the poller can resolve channel + connection
# state without reaching into the bot.
_voice_clients_by_ch: dict[int, object] = {}

_opening_state_lock = threading.Lock()

# Singleton watcher task / loop
_opening_watcher_started: bool = False
_opening_watcher_lock = threading.Lock()
_opening_watcher_loop: Optional[asyncio.AbstractEventLoop] = None

# Generic-ack set for _is_generic_ack. All compared lowercase, stripped of
# trailing punctuation.
_GENERIC_ACK_SET = {
    "yeah", "ok", "okay", "mhm", "uh huh", "uhhuh", "right", "true",
    "exactly", "lol", "lmao", "haha", "thanks", "thank you",
    "yes", "no", "nope", "yep", "sure", "cool", "nice",
    # Whisper's infamous silence hallucinations:
    "thank you.", "thanks for watching.", "thanks for watching",
    "you", "bye", "bye.",
}

_PUNCT_EMOJI_ONLY = re.compile(
    r"^[\s\.\,\!\?\-\_\:\;\"\'\(\)\[\]\{\}\*\~\`\/\\"
    r"\u2000-\u206F\u2E00-\u2E7F"
    r"\U0001F300-\U0001FAFF\U0001F600-\U0001F64F"
    r"\u2600-\u27BF]*$"
)


def _is_generic_ack(text: str) -> bool:
    """True if `text` is a short low-info utterance we shouldn't treat as a
    substantive turn-ending line (acks, whisper hallucinations, emoji-only)."""
    if not text:
        return True
    stripped = text.strip()
    if len(stripped) < VOICE_OPENING_MIN_TEXT_LEN:
        return True
    # Normalise: lowercase + strip surrounding punctuation
    norm = stripped.lower().strip(" .,!?-_:;\"'")
    if norm in _GENERIC_ACK_SET:
        return True
    if stripped.lower() in _GENERIC_ACK_SET:
        return True
    # Punctuation / emoji only
    if _PUNCT_EMOJI_ONLY.match(stripped):
        return True
    return False


def _record_substantive_utterance(ch_id: int, who: str, text: str,
                                  user_id, ts: float) -> None:
    """Record a substantive utterance for opening detection. Safe to call
    from the flusher thread."""
    with _opening_state_lock:
        _last_substantive_ts[ch_id] = ts
        lines = _last_substantive_lines[ch_id]
        lines.append({
            "name": who,
            "text": text,
            "ts": ts,
            "user_id": str(user_id),
        })
        # Trim to last 10
        if len(lines) > 10:
            del lines[:-10]
        _last_substantive_lines[ch_id] = lines
    print(
        f"[nexus_listen.opening] substantive ch={ch_id} user={who} "
        f"ts={ts:.1f} len={len(text)}",
        flush=True,
    )


def _remember_voice_client(vc) -> None:
    """Track a voice_client by its channel id so the poller can resolve it
    without touching nexus_bot. Called from the sink on first audio."""
    try:
        ch = getattr(vc, "channel", None)
        if ch is None:
            return
        _voice_clients_by_ch[ch.id] = vc
    except Exception:
        pass


def _forget_voice_channel(ch_id: int) -> None:
    """Clear all opening state for a channel — call when bot leaves VC."""
    with _opening_state_lock:
        _last_substantive_ts.pop(ch_id, None)
        _last_substantive_lines.pop(ch_id, None)
        _last_opening_fire_ts.pop(ch_id, None)
        _fired_for_last_ts.pop(ch_id, None)
        _voice_clients_by_ch.pop(ch_id, None)


def register_opening_handler(callback) -> None:
    """Register an async callable invoked when a natural opening is detected.

    Signature: async def callback(voice_channel, recent_lines: list[dict]) -> None
    where recent_lines = [{"name": str, "text": str, "ts": float,
                           "user_id": str}, ...]
    representing the last ~10 substantive utterances in this voice session.

    Registering the first handler auto-starts the polling watcher on whatever
    asyncio loop is current at call time (expected: the bot's main loop).
    Subsequent registrations are no-ops for watcher start.
    """
    if callback in _opening_handlers:
        return
    _opening_handlers.append(callback)
    # Auto-start watcher idempotently on first registration.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    _install_opening_watcher(loop)
    print(
        f"[nexus_listen.opening] handler registered "
        f"(total={len(_opening_handlers)})",
        flush=True,
    )


async def _opening_watcher_coro() -> None:
    """Polling loop: every ~1s, check each tracked channel for a silence
    window past threshold, and fire registered handlers."""
    print("[nexus_listen.opening] watcher loop started", flush=True)
    while True:
        try:
            await asyncio.sleep(1.0)
            now = time.time()
            # Snapshot state under lock so we can iterate without races.
            with _opening_state_lock:
                snapshot = list(_last_substantive_ts.items())
            for ch_id, last_ts in snapshot:
                if now - last_ts < VOICE_OPENING_SILENCE_S:
                    continue
                # Already fired for this exact utterance? Skip until re-arm.
                if _fired_for_last_ts.get(ch_id) == last_ts:
                    continue
                # Cooldown gate
                if now - _last_opening_fire_ts.get(ch_id, 0) < VOICE_OPENING_COOLDOWN_S:
                    continue
                # Resolve voice client + connection
                vc = _voice_clients_by_ch.get(ch_id)
                if vc is None:
                    continue
                try:
                    if not vc.is_connected():
                        continue
                except Exception:
                    continue
                ch = getattr(vc, "channel", None)
                if ch is None:
                    continue
                # Fire.
                with _opening_state_lock:
                    lines = list(_last_substantive_lines.get(ch_id, []))
                    _last_opening_fire_ts[ch_id] = now
                    _fired_for_last_ts[ch_id] = last_ts
                handlers = list(_opening_handlers)
                print(
                    f"[nexus_listen.opening] FIRE ch={ch_id} "
                    f"handlers={len(handlers)} lines={len(lines)}",
                    flush=True,
                )
                for cb in handlers:
                    try:
                        await cb(ch, lines)
                    except Exception as e:
                        print(
                            f"[nexus_listen.opening] handler error: "
                            f"{type(e).__name__}: {e}",
                            flush=True,
                        )
        except asyncio.CancelledError:
            print("[nexus_listen.opening] watcher cancelled", flush=True)
            raise
        except Exception as e:
            print(
                f"[nexus_listen.opening] watcher loop error: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )


def _install_opening_watcher(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Idempotently start the opening-detection poller on the given loop.

    If loop is None, uses the currently-running loop. Safe to call many times;
    only the first call actually schedules the task.
    """
    global _opening_watcher_started, _opening_watcher_loop
    with _opening_watcher_lock:
        if _opening_watcher_started:
            return
        try:
            target_loop = loop or asyncio.get_event_loop()
            if target_loop.is_running():
                target_loop.create_task(_opening_watcher_coro())
            else:
                # Loop not yet running — schedule via call_soon_threadsafe
                # once someone runs it. We still mark as started so we don't
                # double-schedule.
                asyncio.run_coroutine_threadsafe(
                    _opening_watcher_coro(), target_loop
                )
            _opening_watcher_loop = target_loop
            _opening_watcher_started = True
            print(
                "[nexus_listen.opening] watcher scheduled "
                f"(silence={VOICE_OPENING_SILENCE_S}s "
                f"cooldown={VOICE_OPENING_COOLDOWN_S}s "
                f"min_len={VOICE_OPENING_MIN_TEXT_LEN})",
                flush=True,
            )
        except Exception as e:
            print(
                f"[nexus_listen.opening] watcher install error: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Passive transcript log — every voice utterance lands here, free.
# This is the "always listening, zero-API-cost" memory layer.
# Format: one JSON object per line, append-only.
# ---------------------------------------------------------------------------
TRANSCRIPT_LOG = Path(__file__).parent / "voice_transcripts.jsonl"
_transcript_log_lock = threading.Lock()


def _append_transcript_log(user_id: int, who: str, text: str, dur_s: float,
                           triggered: Optional[object] = None) -> None:
    """Append a transcript line to voice_transcripts.jsonl. Best-effort, never raises.

    `triggered` override: when None (default), auto-sets from the \\bnexus\\b
    regex on `text` — preserves original behavior. Pass a string like
    "namo" to explicitly tag NAMO interjects so reviewers can see timing.
    """
    try:
        import json
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "user_id": user_id,
            "name": who,
            "text": text,
            "dur_s": round(dur_s, 2),
            "triggered": (
                triggered if triggered is not None
                else bool(_NAME_TRIGGER.search(text))
            ),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _transcript_log_lock:
            with open(TRANSCRIPT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print(f"[nexus_listen.transcript_log] error: {type(e).__name__}: {e}",
              flush=True)


class NexusAudioSink(voice_recv.AudioSink):
    """
    Captures PCM per user, flushes on silence, calls `on_transcript(user, text)`
    from the main asyncio loop whenever a buffer transcribes to non-empty text.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 on_transcript: Callable[[discord.Member, str], "asyncio.Future"],
                 interject_cb: Optional[
                     Callable[[discord.Member, str], "asyncio.Future"]
                 ] = None):
        super().__init__()
        self._loop = loop
        self._on_transcript = on_transcript
        # NAMO interject callback — nullable. When None, NAMO is inert on this
        # sink regardless of the module-level mode flag.
        self._interject_cb = interject_cb
        self._buffers: dict[int, bytearray] = defaultdict(bytearray)
        self._last_heard: dict[int, float] = {}
        self._cooldown_until: dict[int, float] = {}
        self._users: dict[int, discord.Member] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        # --- NAMO state (only touched when interject mode is ON) ---
        self._namo_last_utter_ts: float = 0.0
        self._namo_last_utter_text: str = ""
        self._namo_last_utter_user: Optional[discord.Member] = None
        self._namo_last_utter_pending: bool = False
        self._namo_last_interject_ts: float = 0.0
        self._namo_last_name_trigger_ts: float = 0.0
        self._namo_interjects: int = 0
        self._namo_suppressed: int = 0
        self._namo_last_stats_log: float = time.time()
        # Spawn flusher thread
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

    def wants_opus(self) -> bool:
        return False  # give us decoded PCM

    def write(self, user: Optional[discord.Member], data) -> None:
        if user is None:
            # Unmapped SSRC — can't attribute. Log once per minute so we know
            # audio is arriving even without a user mapping.
            now = time.time()
            if now - getattr(self, "_last_unmapped_log", 0) > 60:
                self._last_unmapped_log = now
                pcm = getattr(data, "pcm", None) or b""
                print(f"[nexus_listen.debug] audio from UNMAPPED ssrc "
                      f"pcm_len={len(pcm)}", flush=True)
            return
        now = time.time()
        if now < self._cooldown_until.get(user.id, 0):
            return  # in cooldown, drop audio
        pcm = getattr(data, "pcm", None) or getattr(data, "data", None)
        # --- debug: first-packet-per-user trace
        if user.id not in self._users:
            pcm_len = len(pcm) if pcm else 0
            ssrc = "?"
            try:
                pkt = getattr(data, "packet", None)
                if pkt is not None:
                    ssrc = getattr(pkt, "ssrc", "?")
            except Exception:
                pass
            print(f"[nexus_listen.debug] first audio user={user.display_name} "
                  f"id={user.id} ssrc={ssrc} pcm_len={pcm_len}", flush=True)
            self._users[user.id] = user
        if not pcm:
            return
        with self._lock:
            self._buffers[user.id].extend(pcm)
            self._last_heard[user.id] = now
            self._users[user.id] = user

    def cleanup(self) -> None:
        self._stopped.set()
        # Flush whatever remains
        self._drain_all()
        # Clear opening-detection state for this voice channel so a stale
        # timestamp can't cause a false fire after leave/rejoin.
        try:
            vc = getattr(self, "voice_client", None)
            ch = getattr(vc, "channel", None) if vc is not None else None
            if ch is not None:
                _forget_voice_channel(ch.id)
        except Exception:
            pass

    def start_cooldown(self, user_id: int, seconds: float = COOLDOWN_SECONDS):
        self._cooldown_until[user_id] = time.time() + seconds

    # --- internals ---
    def _flush_loop(self):
        while not self._stopped.is_set():
            time.sleep(0.25)
            try:
                self._tick()
            except Exception as e:
                print(f"[nexus_listen.flush_loop] error: {type(e).__name__}: {e}")

    def _tick(self):
        now = time.time()
        to_flush: list[tuple[int, bytes]] = []
        with self._lock:
            for uid, last in list(self._last_heard.items()):
                buf = self._buffers.get(uid)
                if not buf:
                    continue
                if now - last >= SILENCE_SECONDS:
                    # Enough silence — flush
                    duration = len(buf) / BYTES_PER_SECOND
                    # Consent gate — skip transcription for opted-out users
                    # and during any active mute window (user-specific or global).
                    skip_reason = None
                    try:
                        import nexus_consent as _nc
                        if _nc.is_opted_out(uid):
                            skip_reason = "user opted out"
                        elif _nc.is_muted_now(uid) or _nc.is_muted_now(None):
                            skip_reason = "muted"
                    except Exception:
                        pass

                    if skip_reason:
                        print(f"[nexus_listen.flush] uid={uid} dur={duration:.2f}s "
                              f"-> dropped ({skip_reason})", flush=True)
                    elif duration >= MIN_BUFFER_SECONDS:
                        to_flush.append((uid, bytes(buf)))
                        print(f"[nexus_listen.flush] uid={uid} dur={duration:.2f}s "
                              f"-> transcribe", flush=True)
                    else:
                        print(f"[nexus_listen.flush] uid={uid} dur={duration:.2f}s "
                              f"-> too short, dropped", flush=True)
                    # Clear buffer regardless
                    self._buffers[uid] = bytearray()
                    self._last_heard.pop(uid, None)
        for uid, pcm in to_flush:
            self._process_flush(uid, pcm)

        # --- NAMO turn-open detection + stats log ---
        # Gated on interject mode so the OFF path is a no-op: no eval, no log.
        if _is_interject_mode_on() and self._interject_cb is not None:
            if (self._namo_last_utter_pending
                    and now - self._namo_last_utter_ts >= NAMO_PAUSE_SECONDS):
                try:
                    self._evaluate_namo(now)
                except Exception as e:
                    print(
                        f"[nexus_listen.namo] eval error: "
                        f"{type(e).__name__}: {e}",
                        flush=True,
                    )
                finally:
                    self._namo_last_utter_pending = False

            # 10s stats log — extended with NAMO counters for review visibility.
            if now - self._namo_last_stats_log >= 10.0:
                self._namo_last_stats_log = now
                print(
                    f"[nexus_listen.stats] interjects={self._namo_interjects} "
                    f"suppressed={self._namo_suppressed} "
                    f"mode=ON",
                    flush=True,
                )

    def _evaluate_namo(self, now: float) -> None:
        """Check gates and fire `interject_cb` if all pass.

        Increments `_namo_suppressed` for any pause event that fails a gate.
        """
        text = self._namo_last_utter_text
        user = self._namo_last_utter_user
        if not text or user is None:
            self._namo_suppressed += 1
            return

        # Gate 1: word count must exceed NAMO_MIN_WORDS (strictly > 6).
        word_count = len(text.split())
        if word_count <= NAMO_MIN_WORDS:
            self._namo_suppressed += 1
            return

        # Gate 2: utterance must contain at least one signal marker.
        if not _NAMO_SIGNAL.search(text):
            self._namo_suppressed += 1
            return

        # Gate 3: rate limit — at most 1 interject per NAMO_COOLDOWN_SECONDS.
        if now - self._namo_last_interject_ts < NAMO_COOLDOWN_SECONDS:
            self._namo_suppressed += 1
            return

        # Gate 4: holdoff after any name-trigger reply (10s).
        if now - self._namo_last_name_trigger_ts < NAMO_NAME_TRIGGER_HOLDOFF:
            self._namo_suppressed += 1
            return

        # Gate 5: never fire while the bot is already speaking.
        vc = getattr(self, "voice_client", None)
        try:
            if vc is not None and vc.is_playing():
                self._namo_suppressed += 1
                return
        except Exception:
            # If is_playing() blows up, be conservative and suppress.
            self._namo_suppressed += 1
            return

        # All gates passed — fire the interject.
        self._namo_last_interject_ts = now
        self._namo_interjects += 1
        dur_s = float(len(text.split())) * 0.35  # rough — actual buffer gone
        _append_transcript_log(
            user.id, user.display_name, text, dur_s, triggered="namo"
        )
        print(
            f"[nexus_listen.namo] FIRE user={user.display_name} "
            f"words={word_count} text={text!r}",
            flush=True,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._interject_cb(user, text), self._loop
            )
            _ = fut
        except Exception as e:
            print(
                f"[nexus_listen.namo] dispatch error: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

    def _drain_all(self):
        with self._lock:
            items = [(uid, bytes(buf)) for uid, buf in self._buffers.items() if buf]
            self._buffers.clear()
            self._last_heard.clear()
        for uid, pcm in items:
            if len(pcm) / BYTES_PER_SECOND >= MIN_BUFFER_SECONDS:
                self._process_flush(uid, pcm)

    def _process_flush(self, user_id: int, pcm: bytes):
        user = self._users.get(user_id)
        path = TEMP_DIR / f"vc_{user_id}_{uuid.uuid4().hex}.wav"
        # Audio stats — peak + RMS so we can spot silent/low-volume issues
        try:
            import audioop
            peak = audioop.max(pcm, PCM_SAMPWIDTH)
            rms = audioop.rms(pcm, PCM_SAMPWIDTH)
            print(f"[nexus_listen.audio] uid={user_id} bytes={len(pcm)} "
                  f"peak={peak} rms={rms}", flush=True)
        except Exception as e:
            print(f"[nexus_listen.audio] stats error: {e}", flush=True)
        try:
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(PCM_CHANNELS)
                wf.setsampwidth(PCM_SAMPWIDTH)
                wf.setframerate(PCM_FRAME_RATE)
                wf.writeframes(pcm)
            # Keep a rolling debug sample (last DEBUG_WAV_KEEP files only).
            # Disable entirely with config.LISTEN_KEEP_DEBUG_WAVS = False.
            if getattr(config, "LISTEN_KEEP_DEBUG_WAVS", True):
                debug_path = TEMP_DIR / f"debug_{user_id}_{int(time.time())}.wav"
                try:
                    import shutil
                    shutil.copy(str(path), str(debug_path))
                    # Cap retention — prune oldest beyond DEBUG_WAV_KEEP
                    keep = int(getattr(config, "LISTEN_DEBUG_WAV_KEEP", 20))
                    debugs = sorted(
                        TEMP_DIR.glob("debug_*.wav"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    for stale in debugs[:-keep]:
                        try:
                            stale.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass
            text = transcribe_wav(str(path))
        finally:
            try:
                path.unlink()
            except Exception:
                pass

        who = user.display_name if user else f"user_{user_id}"
        dur_s = len(pcm) / BYTES_PER_SECOND
        if not text:
            print(f"[nexus_listen] {who}: <empty> (buf={dur_s:.1f}s)", flush=True)
            return
        print(f"[nexus_listen] {who}: {text}  (buf={dur_s:.1f}s)", flush=True)

        # ALWAYS log transcript locally — passive memory, zero API cost.
        # This is the "always listening for free" channel: every spoken
        # utterance lands in voice_transcripts.jsonl regardless of trigger.
        _append_transcript_log(user_id, who, text, dur_s)

        # --- Opening detection hook (Agent D) ---
        # Record substantive utterances for natural-opening detection. This
        # is additive: the polling watcher (in asyncio) decides when to fire
        # handlers based on silence + cooldown. Voice-client tracking keeps
        # the watcher decoupled from nexus_bot.
        try:
            vc = getattr(self, "voice_client", None)
            ch = getattr(vc, "channel", None) if vc is not None else None
            if (user is not None and ch is not None
                    and len(text) >= VOICE_OPENING_MIN_TEXT_LEN
                    and not _is_generic_ack(text)):
                _remember_voice_client(vc)
                _record_substantive_utterance(
                    ch.id, who, text, user_id, time.time()
                )
        except Exception as e:
            print(
                f"[nexus_listen.opening] record error: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

        # Write to mem0 in the background. remember() does its own min-char
        # gate + consent check + classifier (LLM) + mem0.add (LLM), which is
        # WAY too slow for the flusher hot path — fire-and-forget on a daemon
        # so we never block whisper. channel="voice" so we can distinguish.
        if user is not None:
            def _bg_remember(uid_s=str(user_id), uname=who, msg=text):
                try:
                    import nexus_brain
                    nexus_brain.remember(uid_s, uname, "voice", msg)
                except Exception as e:
                    print(f"[nexus_listen.remember] error: "
                          f"{type(e).__name__}: {e}", flush=True)
            threading.Thread(target=_bg_remember, daemon=True).start()

        # Only trigger the Claude reply pipeline if Nexus is addressed by name.
        # The trigger is a word-boundary "nexus" match — "hey nexus", "yo nexus",
        # "nexus what do you think" all qualify; passive chatter does not.
        if _NAME_TRIGGER.search(text):
            if user is not None:
                # Put the user into cooldown so we don't retrigger on the same utterance
                self.start_cooldown(user_id)
                # NAMO bookkeeping only — never fire NAMO within 10s of a name
                # trigger reply. Assignment is a no-op when NAMO is OFF.
                self._namo_last_name_trigger_ts = time.time()
                fut = asyncio.run_coroutine_threadsafe(
                    self._on_transcript(user, text), self._loop
                )
                # Don't block on result; let it run
                _ = fut
        else:
            # NAMO candidate: non-trigger utterance just ended. Only record if
            # interject mode is ON AND this sink has a callback wired. When
            # OFF, zero state accumulates and the original behavior is preserved.
            if (user is not None
                    and self._interject_cb is not None
                    and _is_interject_mode_on()):
                self._namo_last_utter_ts = time.time()
                self._namo_last_utter_text = text
                self._namo_last_utter_user = user
                self._namo_last_utter_pending = True


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
def clear_temp():
    for p in TEMP_DIR.glob("*.wav"):
        try:
            p.unlink()
        except Exception:
            pass
