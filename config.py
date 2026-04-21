"""
TNC / Nexus configuration — channel names, role names, behavior constants.
Everything in here is editable without touching bot logic.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
PERSONA_FILE = ROOT / "persona.md"
MEM0_DATA_DIR = ROOT / "mem0_data"
LOG_DIR = ROOT / "logs"

# ---------------------------------------------------------------------------
# Discord — roles & channels (must match setup_server.py)
# ---------------------------------------------------------------------------
ROLE_VOID = "Void"
ROLE_SIGNAL = "Signal"
ROLE_ARCHITECT = "Architect"
ROLE_COPILOT = "Co-pilot"
ROLE_FOUNDER = "Founder"

CHANNEL_FIRST_LIGHT = "welcome"           # landing channel, welcome posts here
CHANNEL_CHARTER = "rules"                 # rules pin
CHANNEL_NEW_SIGNAL = "intros"             # intros — Void can post here only
CHANNEL_THESIS = "goals"                  # goals pin
CHANNEL_COMMANDS = "commands"             # bot command testing
CHANNEL_DEV_LOGS = "logs"                 # nexus build log, Architect+ only
CHANNEL_THOUGHTS = "thoughts"             # nexus's public thought stream

CATEGORY_INNER_CIRCLE = "🔒 INNER CIRCLE"  # category dev-logs lives under (if exists)


def canon_channel(name: str) -> str:
    """Return the logical name of a channel (strips emoji prefix).

    Channel names after facelift look like '👋│welcome' — match code uses
    plain names ('welcome'), so canon strips the prefix up to and including
    the separator.
    """
    if not name:
        return ""
    for sep in ("│", "・", "｜", "|"):
        if sep in name:
            return name.split(sep, 1)[1].lower()
    return name.lower()


# Channels Nexus actively listens in + threads across (Signal+ only).
# Use plain (post-canon) names.
NEXUS_LISTEN_CHANNELS = {
    "memory-lab", "dispatches",
    "geni", "eft-companion", "music-lab", "scrapyard",
    "chat", "tangents", "dopamine",
    "the-table", "logs", "builds",
    "commands",
}

# Channels to ignore entirely (entry / low-signal). Plain (post-canon) names.
NEXUS_IGNORE_CHANNELS = {
    "welcome", "rules", "intros", "goals", "thoughts",
}

# ---------------------------------------------------------------------------
# Promotion flow
# ---------------------------------------------------------------------------
# Who can react ✅ in #new-signal to promote a Void → Signal
PROMOTION_REACTORS = {ROLE_ARCHITECT, ROLE_COPILOT, ROLE_FOUNDER}
PROMOTION_EMOJI = "✅"

# ---------------------------------------------------------------------------
# Nexus behavior
# ---------------------------------------------------------------------------
# Claude model for replies
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 800

# How much recent channel context to include in replies (default window)
RECENT_CONTEXT_MESSAGES = 15

# When the trigger msg smells like a catch-up ("what happened", "check chat",
# etc.), expand the channel-history window by this much. Keeps normal replies
# cheap while letting "nexus what just went down" actually work.
CATCHUP_CONTEXT_EXTRA = 15

# How many memories to retrieve per query
MEM0_TOP_K = 8

# Messages shorter than this aren't stored as memories (spam filter)
MIN_MESSAGE_CHARS_FOR_MEMORY = 20

# Phase 2: threading — how often to proactively thread (1 in N substantive msgs)
THREADING_RATE = 8

# ---------------------------------------------------------------------------
# Listen mode (Phase 3) — voice receive + Whisper transcription
# ---------------------------------------------------------------------------
WHISPER_MODEL = "small"       # tiny | base | small | medium | large-v3
WHISPER_DEVICE = "cpu"        # "cuda" to use the 4070 Ti
WHISPER_COMPUTE_TYPE = "int8" # "float16" on GPU, "int8" on CPU

# Silence gap (seconds) after which a user's audio buffer is flushed → transcribed
LISTEN_SILENCE_SECONDS = 1.2
# Minimum buffer duration to bother transcribing (drops noise blips)
LISTEN_MIN_BUFFER_SECONDS = 0.6
# After a triggered reply, ignore that user's audio for this many seconds
LISTEN_COOLDOWN_SECONDS = 4.0

# If set, Nexus also posts a text transcript of voice conversations here.
# Set to None to keep voice conversations voice-only.
VOICE_LOG_CHANNEL = "commands"
