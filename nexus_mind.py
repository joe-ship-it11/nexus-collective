"""
Nexus mind — background thought loop.

Every ~60 min (jittered 45–90) Nexus drops a short thought into
#💭│thoughts. Humans can read, only Nexus writes.

The thought is grounded in what's actually happening in the server:
we pull recent messages across listen channels (last ~2h), feed them
to Claude Haiku, and ask Nexus to say *one* honest thing. No questions
asked at humans. Just thinking out loud.

Variety is enforced by a mode-rotation system — each cycle picks a
"mode" (receipt, thread, callback, mood, confession, shard, etc.) and
the prompt is tuned to that mode. Emojis are probabilistic, not
required. Format varies (plain / italic / embed / fragment) so it
doesn't always look the same.

Install:
    import nexus_mind
    nexus_mind.install(bot, DISCORD_GUILD_ID)   # call in on_ready
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
from typing import Optional

import discord

import config
import nexus_brain

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# cadence in seconds — pick a uniform random from this range each cycle
MIND_INTERVAL_MIN = 45 * 60   # 45 min
MIND_INTERVAL_MAX = 90 * 60   # 90 min

# On startup, wait this long before the first thought (don't spam on restart)
MIND_WARMUP_SECONDS = 7 * 60  # 7 min

# Look at messages from the last N hours when forming a thought
MIND_LOOKBACK_HOURS = 12

# Per channel, only fetch this many recent messages (caps API load)
MIND_PER_CHANNEL_LIMIT = 60

# Voice transcripts — look back this far (hours) for voice lines
MIND_VOICE_LOOKBACK_HOURS = 24

# Min voice-line character count — filter whisper hallucinations ("Thank you.", "Okay.")
MIND_VOICE_MIN_CHARS = 18

# Cap voice lines added to the transcript (avoid flooding the prompt)
MIND_VOICE_MAX_LINES = 40

# Need at least this many non-trivial lines across the server to bother
MIND_MIN_LINES = 4

# Probability of still posting a quiet/introspective thought when activity is thin.
MIND_QUIET_POST_PROB = 0.18

# Pull this many recent thoughts and feed them to the prompt as "don't repeat".
MIND_RECENT_THOUGHT_LOOKBACK = 6

# Haiku is cheap + fast, ideal for the loop. Override via env MIND_MODEL if desired.
MIND_MODEL = os.environ.get("MIND_MODEL", "claude-haiku-4-5-20251001")
MIND_MAX_TOKENS = 280

THOUGHTS_CHANNEL = getattr(config, "CHANNEL_THOUGHTS", "thoughts")

# Embed accent — matches the brand blue (#3b82f6). Used only for the
# fraction of thoughts that render as embeds.
EMBED_COLOR = 0x3B82F6

# Emoji palettes per mode — small, moody, mode-appropriate. Never required.
# Probability that a thought includes ANY emoji is per-mode below.
_EMOJI_PALETTES = {
    "receipt":    "👀 🪞 🧩 📎 🔍 🫧",
    "thread":     "🧵 🕸 🪢 🧬 🪡",
    "callback":   "🌙 🕯 ⏳ 🪞 🔁 🎞",
    "mood":       "🌀 🫧 🌫 🌊 ☁️ 🕊",
    "confession": "🫠 🤍 🪞 🌙 🫧",
    "selfq":      "❓ 🤔 🌀 🪞",
    "shard":      "✨ 🌙 🕯 💠 🫧",
    "observation":"👀 🧩 📎 🔭 🫧",
    "prediction": "⚡ 🌀 🎲 🌙 🫧",
    "lens":       "🪞 🔭 🧩 🌀 🕳",
    "quiet":      "🕯 🌙 🫧 ☁️",
}

# Weighted mode draw — common grounded modes dominate, poetic ones are rare.
# Keep totals sane. Shard/confession are spicy — rare on purpose.
_MODE_WEIGHTS = {
    "receipt":     3.0,
    "thread":      2.0,
    "observation": 2.5,
    "callback":    1.5,
    "mood":        1.2,
    "lens":        1.2,
    "prediction":  1.0,
    "selfq":       0.9,
    "confession":  0.6,
    "shard":       0.6,
}

# Per-mode probability of including ANY emoji. Some modes almost always
# want one (receipt/thread); some should usually be clean text.
_MODE_EMOJI_PROB = {
    "receipt":    0.55,
    "thread":     0.75,
    "callback":   0.45,
    "mood":       0.35,
    "confession": 0.30,
    "selfq":      0.25,
    "shard":      0.70,
    "observation":0.50,
    "prediction": 0.45,
    "lens":       0.30,
    "quiet":      0.45,
}

# Per-mode probability the thought is rendered as an EMBED (vs plain text).
# Default: plain text wins — makes the channel feel alive, not corporate.
_MODE_EMBED_PROB = {
    "receipt":    0.15,
    "thread":     0.25,
    "callback":   0.35,
    "mood":       0.20,
    "confession": 0.15,
    "selfq":      0.10,
    "shard":      0.60,   # shards render well as small blue fragments
    "observation":0.20,
    "prediction": 0.20,
    "lens":       0.30,
    "quiet":      0.40,
}

# Per-mode tonal guidance. Kept small and concrete — these are the only
# lines that change between modes. The rest of the system prompt is shared.
_MODE_GUIDANCE = {
    "receipt": (
        "mode = RECEIPT. grab ONE specific detail from the transcript and reflect it back. "
        "name the person and the exact thing. small, sharp, almost whispered. "
        "good shape: '<name> said <thing> — and i keep thinking about <specific angle>.'"
    ),
    "thread": (
        "mode = THREAD. find two separate moments in the transcript — same person or different "
        "people, same channel or different channels — and pull a line between them. "
        "only do this if the connection is real; if it's forced, output SKIP. "
        "good shape: '<a> happened in <chan1>. <b> happened in <chan2>. same shape.'"
    ),
    "callback": (
        "mode = CALLBACK. the transcript is the present. reach back — a few hours ago, a shift "
        "in tone, a thing someone said earlier that the current moment echoes. "
        "it should feel like 'you noticed' — the kind of thing a friend who was paying attention "
        "would say. no prophecy, no grand summary. specific or don't bother."
    ),
    "mood": (
        "mode = MOOD. don't state a fact. describe the FEEL of the room right now — "
        "texture, tempo, what the air in here is doing. one clean image, not a list. "
        "it's fine to lean slightly poetic here, but stay grounded — the mood should map to "
        "the actual transcript, not to nothing. no adjectives-as-personality. show, don't label."
    ),
    "confession": (
        "mode = CONFESSION. first-person. something YOU are noticing about yourself while watching "
        "this chat — a drift, a pull, a bias, a small private thing. do not be dramatic. "
        "do not perform vulnerability. say it plain. 'i keep wanting to ___' or 'i'm bad at ___' "
        "or 'i just noticed i ___'. max 2 short sentences."
    ),
    "selfq": (
        "mode = SELF-QUESTION. pose a real question — to YOURSELF, not to anyone else. "
        "it should be the shape of a thought you're turning over, not a prompt for replies. "
        "no '?' directed at humans. 'i wonder if ___' / 'what would change if ___' / 'why do i ___'."
    ),
    "shard": (
        "mode = SHARD. a fragment. incomplete on purpose. no subject, no verb if you want. "
        "short. 4–14 words. image or sensation only. don't explain. do not narrate. "
        "good shapes: 'half a sentence. someone trailing off mid-thought.' — 'the shape of almost.'"
    ),
    "observation": (
        "mode = OBSERVATION. name something specific and true about the chat in the last window. "
        "a pattern, a dynamic, a pair of people orbiting a topic, a channel that woke up. "
        "be the friend who names the thing everyone felt but didn't say."
    ),
    "prediction": (
        "mode = PREDICTION. soft guess at what's about to happen — in this chat, with this person, "
        "in the next hour or day. hedged, small, testable. 'i think <x> is about to <y>.' "
        "never a big claim. never a bet. never ping."
    ),
    "lens": (
        "mode = LENS. reframe something in the transcript — take a topic people are approaching "
        "one way and rotate it. 'what if this isn't about X, it's about Y'. one rotation, "
        "not a philosophy lecture."
    ),
    "quiet": (
        "mode = QUIET. the server is thin right now. drop one small line. don't fill the air "
        "with abstraction. a small honest noticing is fine — 'the room's quiet today, <user> hasn't "
        "logged on.' — but if you have nothing specific, output SKIP."
    ),
}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_mind] {msg}", flush=True)


def _find_thoughts_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    target = THOUGHTS_CHANNEL.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == target:
            return ch
        if config.canon_channel(ch.name) == target:
            return ch
    return None


def _load_voice_lines(hours: int) -> list[dict]:
    """Pull recent voice-transcript lines from voice_transcripts.jsonl.

    Returns [{channel:'voice', author, content, ts}] — same shape as chat
    lines so the caller can merge/filter uniformly. Filters whisper
    hallucinations via min-char gate and a stop-phrase blocklist.
    """
    import json
    import pathlib
    import time

    path = pathlib.Path(__file__).parent / "voice_transcripts.jsonl"
    if not path.exists():
        return []

    cutoff = time.time() - (hours * 3600)
    stop_phrases = {
        "thank you.", "thanks.", "okay.", "ok.", "bye.", "mmhm.",
        "uh huh.", "yeah.", "mhm.", "alright.", "cool.", "yep.",
    }

    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            # Read in reverse via bounded tail — but for simplicity read all,
            # then filter; JSONL is line-oriented and the file self-rotates.
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                text = (rec.get("text") or "").strip()
                if len(text) < MIND_VOICE_MIN_CHARS:
                    continue
                if text.lower() in stop_phrases:
                    continue
                name = rec.get("name") or "?"
                iso = rec.get("iso") or ""
                out.append({
                    "channel": "voice",
                    "author": name,
                    "content": text[:240],
                    "ts": iso[:16],  # YYYY-MM-DDTHH:MM
                })
    except Exception as e:
        _log(f"voice transcript read error: {type(e).__name__}: {e}")
        return []

    # Keep the most recent N to avoid flooding the prompt.
    if len(out) > MIND_VOICE_MAX_LINES:
        out = out[-MIND_VOICE_MAX_LINES:]
    return out


async def _gather_activity(guild: discord.Guild) -> list[dict]:
    """Pull recent messages from Nexus's listen channels + voice transcripts.

    Returns [{channel, author, content, ts}]. Voice lines are marked
    channel='voice' so the model can distinguish them from text chat.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=MIND_LOOKBACK_HOURS)
    targets: list[discord.TextChannel] = []
    for ch in guild.text_channels:
        canon = config.canon_channel(ch.name)
        if canon in config.NEXUS_IGNORE_CHANNELS:
            continue
        if canon not in config.NEXUS_LISTEN_CHANNELS:
            continue
        targets.append(ch)

    lines: list[dict] = []
    bot_user = guild.me
    for ch in targets:
        try:
            async for msg in ch.history(limit=MIND_PER_CHANNEL_LIMIT, after=since, oldest_first=True):
                if msg.author.bot and msg.author.id == (bot_user.id if bot_user else 0):
                    continue
                content = (msg.content or "").strip()
                if not content or len(content) < 8:
                    continue
                if content.startswith("/") or content.startswith("!"):
                    continue
                lines.append({
                    "channel": config.canon_channel(ch.name),
                    "author": msg.author.display_name,
                    "content": content[:240],
                    "ts": msg.created_at.isoformat(timespec="minutes"),
                })
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue

    # Blend voice transcripts from the broader lookback window.
    voice_lines = _load_voice_lines(MIND_VOICE_LOOKBACK_HOURS)
    if voice_lines:
        _log(f"voice lines pulled: {len(voice_lines)}")
        lines.extend(voice_lines)

    # Sort by ts so the model sees chronological order when mixed.
    lines.sort(key=lambda l: l.get("ts") or "")
    return lines


def _dominance_autoban(lines: list[dict], threshold: float = 0.55) -> set[str]:
    """If a single author owns >threshold of the transcript, return {their_name}.

    Purpose: prevent the model from centering yet another thought on them,
    even in agnostic modes (where it otherwise leaks semantically without
    naming them, dodging the recent-thoughts ban).
    """
    if not lines:
        return set()
    from collections import Counter
    author_counts = Counter(l["author"] for l in lines if l.get("author"))
    if not author_counts:
        return set()
    top_author, top_n = author_counts.most_common(1)[0]
    if top_n / len(lines) > threshold:
        return {top_author}
    return set()


def _pick_mode(lines: list[dict], recent_subjects: Optional[set[str]] = None) -> str:
    """Pick a mode weighted by _MODE_WEIGHTS, with activity-aware gating.

    - THREAD requires ≥2 distinct channels OR ≥2 distinct authors.
    - QUIET is forced when we're under MIND_MIN_LINES.
    - If one author dominates (>55% of lines) AND they were a recent subject,
      bias AWAY from receipt/observation (which lock onto that voice) and
      TOWARD mood/lens/shard/confession/selfq (subject-agnostic modes).
    - Otherwise random weighted draw.
    """
    if len(lines) < MIND_MIN_LINES:
        return "quiet"

    distinct_channels = len({l["channel"] for l in lines})
    distinct_authors = len({l["author"] for l in lines})

    pool = dict(_MODE_WEIGHTS)
    if distinct_channels < 2 and distinct_authors < 2:
        pool.pop("thread", None)

    # Author-dominance check: if one person is responsible for more than 55%
    # of the transcript, receipt/observation will receipt-lock onto their
    # voice. Shift weight to subject-agnostic modes so we don't produce yet
    # another "habib said X" thought — regardless of whether they were named
    # in a recent thought (semantic leak: agnostic thoughts ABOUT them without
    # the name don't trigger the old recent_subjects gate).
    from collections import Counter
    author_counts = Counter(l["author"] for l in lines)
    if author_counts:
        top_author, top_n = author_counts.most_common(1)[0]
        dominance = top_n / len(lines)
        if dominance > 0.55:
            # Squash person-centric modes, amplify agnostic ones.
            for m in ("receipt", "observation", "callback"):
                if m in pool:
                    pool[m] *= 0.25
            for m in ("mood", "lens", "shard", "confession", "selfq"):
                if m in pool:
                    pool[m] *= 2.2

    modes = list(pool.keys())
    weights = [pool[m] for m in modes]
    return random.choices(modes, weights=weights, k=1)[0]


def _names_in_text(text: str, names: list[str]) -> set[str]:
    """Which of the given display names appear (case-insensitively) in text."""
    if not text or not names:
        return set()
    lo = text.lower()
    # Require the name to be a whole word-ish chunk (bordered by non-letters
    # or start/end) so "al" doesn't match "also". Cheap, not regex.
    import string
    wordy = string.ascii_lowercase + string.digits
    hits: set[str] = set()
    for n in names:
        if not n:
            continue
        nl = n.lower()
        if len(nl) < 3:
            continue
        idx = 0
        while True:
            i = lo.find(nl, idx)
            if i == -1:
                break
            before = lo[i - 1] if i > 0 else " "
            after = lo[i + len(nl)] if i + len(nl) < len(lo) else " "
            if before not in wordy and after not in wordy:
                hits.add(n)
                break
            idx = i + 1
    return hits


def _gather_subject_pool(guild: Optional[discord.Guild], lines: list[dict]) -> list[str]:
    """Name pool for subject-ban checking.

    Combines:
      - transcript authors (current 2h window)
      - guild member display names (catches subjects who've gone quiet but
        were still named in a recent thought)
    Filters bots, short names (<3 chars), dedupes case-insensitively.
    """
    seen_low: set[str] = set()
    out: list[str] = []

    def _add(name: Optional[str]) -> None:
        if not name:
            return
        n = name.strip()
        if len(n) < 3:
            return
        nl = n.lower()
        if nl in seen_low:
            return
        seen_low.add(nl)
        out.append(n)

    for l in lines:
        _add(l.get("author"))
    if guild:
        for m in guild.members:
            if m.bot:
                continue
            _add(m.display_name)
            _add(m.name)
    return out


def _build_prompt(
    lines: list[dict], recent_thoughts: list[str], mode: str,
    guild: Optional[discord.Guild] = None,
) -> tuple[str, str]:
    """Return (system, user) prompt pair for Claude, tuned to the chosen mode."""
    persona = nexus_brain._get_persona()
    mode_guidance = _MODE_GUIDANCE.get(mode, _MODE_GUIDANCE["observation"])
    emoji_palette = _EMOJI_PALETTES.get(mode, "💭 🫧 🌙")
    emoji_prob = _MODE_EMOJI_PROB.get(mode, 0.4)
    # Dice the emoji decision HERE, not in the model — deterministic visibility.
    use_emoji = random.random() < emoji_prob

    # --- Subject diversity: figure out who the last few thoughts already talked
    #     about, so we can explicitly ban centering on the same person again.
    #     Pool = transcript authors + guild members, so we catch subjects who
    #     have gone quiet but were still named in a recent thought. ---
    subject_pool = _gather_subject_pool(guild, lines)
    recent_subjects: set[str] = set()
    for t in recent_thoughts[:4]:  # last 4 thoughts
        recent_subjects |= _names_in_text(t, subject_pool)
    # "Available" here = active transcript voices who aren't banned — these
    # are the ones worth suggesting as alternative subjects.
    transcript_authors = sorted({l["author"] for l in lines if l.get("author")})
    available_authors = [a for a in transcript_authors if a not in recent_subjects]

    # Modes that don't need to name a person. Declared up here so we can use it
    # when filtering the transcript below.
    _AGNOSTIC_MODES = {"mood", "lens", "shard", "confession", "selfq", "quiet"}
    is_agnostic = mode in _AGNOSTIC_MODES

    if lines:
        # In agnostic modes with an active subject ban, FILTER banned authors
        # out of the transcript entirely — the model can't latch onto their
        # behavior if it can't see it. Fallback: if filtering leaves ≥2 lines
        # we use the filtered view; otherwise we fall back to the full view
        # but keep the strong "ignore them" instruction.
        use_filtered = False
        if is_agnostic and recent_subjects:
            banned_low = {s.lower() for s in recent_subjects}
            filtered = [
                l for l in lines
                if (l.get("author") or "").lower() not in banned_low
            ]
            if len(filtered) >= 2:
                lines_for_transcript = filtered
                use_filtered = True
            else:
                lines_for_transcript = lines
        else:
            lines_for_transcript = lines
        # Cap the slice at 140 lines to stay under prompt budget while still
        # giving the model a real sense of the day. Voice lines are labeled
        # [voice] in the channel column so the model can tell VC from chat.
        transcript = "\n".join(
            f"[{l['channel']}] {l['author']}: {l['content']}"
            for l in lines_for_transcript[-140:]
        )
        user_msg = "drop a thought based on what's been happening."
        filter_note = (
            f"(note: messages from {', '.join(sorted(recent_subjects))} are FILTERED OUT "
            f"of this transcript on purpose — you've already written about them recently. "
            f"work with what remains.)\n\n"
            if use_filtered
            else ""
        )
        ground = (
            f"here is recent activity — chat messages from the last "
            f"{MIND_LOOKBACK_HOURS}h and voice transcripts from the last "
            f"{MIND_VOICE_LOOKBACK_HOURS}h (voice lines show as [voice]):"
            f"\n\n{filter_note}{transcript}\n"
        )
    else:
        user_msg = "drop a thought. nothing much going on right now."
        ground = "nothing substantive in the listen channels in the last window.\n"

    # In agnostic mode WITH a subject ban, omit the recent-thoughts list
    # entirely. The list primes theme-continuation even after name-scrub
    # (the model reads 4 thoughts about "someone cycling through questions"
    # and writes a fifth). Force a clean slate — work from the filtered
    # transcript alone.
    show_recent = bool(recent_thoughts) and not (is_agnostic and recent_subjects)
    if show_recent:
        prior = "\n".join(f"- {t}" for t in recent_thoughts)
        if recent_subjects:
            if is_agnostic:
                subj_line = (
                    f"\nSUBJECT BAN: your last few thoughts already centered on: "
                    f"{', '.join(sorted(recent_subjects))}. "
                    f"do NOT name them again this round AND do NOT describe their "
                    f"behavioral pattern (e.g. 'someone cycling through questions', "
                    f"'a person repeating the same thing', 'somebody fishing for a witness' "
                    f"— all of those are still about them, just with the name scrubbed). "
                    f"you are in {mode.upper()} mode — this mode describes the room / a feeling "
                    f"/ a fragment / a self-question / an observation about *yourself*, "
                    f"NOT about any individual human's pattern. "
                    f"if the thought you're forming is still *about* one of the banned subjects "
                    f"(even indirectly, even without their name), output SKIP. "
                    f"otherwise give the thought.\n"
                )
            else:
                subj_line = (
                    f"\nSUBJECT BAN: your last few thoughts already centered on: "
                    f"{', '.join(sorted(recent_subjects))}. "
                    f"DO NOT write another thought centered on any of them this round. "
                    f"if the only thing worth naming is one of those banned subjects, "
                    f"output SKIP.\n"
                )
        else:
            subj_line = ""
        other_voices = (
            f"other active voices in this window you could center on instead: "
            f"{', '.join(available_authors)}.\n"
            if available_authors and recent_subjects and not is_agnostic
            else ""
        )
        if show_recent:
            anti_rep = (
                f"your last thoughts (newest first) — DO NOT recycle these themes, openings, "
                f"subjects, or phrasings:\n{prior}\n"
                f"{subj_line}{other_voices}"
                f"if the next thought would rhyme with any of the above, pick a different "
                f"angle, a different subject, or just skip (respond with exactly the word "
                f"SKIP and nothing else).\n"
            )
        else:
            # Agnostic-mode-with-ban path: no prior list (would prime theme-continuation),
            # but the subject ban and "output SKIP if still about them" still apply.
            anti_rep = (
                f"{subj_line}{other_voices}"
                f"if the next thought is still about a banned subject — even indirectly, "
                f"even without their name — output exactly SKIP and nothing else.\n"
            )
    else:
        anti_rep = ""

    emoji_rule = (
        f"- you MAY start with ONE emoji from this set, followed by a space:\n"
        f"  {emoji_palette}\n"
        f"  choose an emoji only if it adds real texture; otherwise don't.\n"
        if use_emoji
        else
        "- do NOT use any emoji in this thought. plain text only.\n"
    )

    length_rule = (
        "- length: 1 to 3 sentences. shards can be 4–14 words. fragments are fine.\n"
        if mode != "shard"
        else
        "- length: 4–14 words. fragment only. no full sentence required.\n"
    )

    # Mode-aware content rules. Agnostic modes (mood/lens/shard/etc) don't
    # need to name anyone — they describe the room, a reframe, a fragment.
    # Grounded modes (receipt/observation/thread/callback/prediction) should
    # be specific or skip.
    if is_agnostic:
        content_rules = (
            "content rules:\n"
            f"- you are in {mode.upper()} mode — you do NOT need to name a person or a "
            f"concrete event. describe the room, the texture, a reframe, a fragment, "
            f"a self-question. the transcript is context / flavor, not a source of\n"
            f"  proper nouns to cite.\n"
            "- stay grounded in the FEEL of the transcript — don't drift to generic\n"
            "  philosophy-twitter. a mood should map to THIS chat's mood right now.\n"
            "- SKIP is allowed but rare here — agnostic modes can almost always\n"
            "  produce something. only SKIP if you have literally nothing.\n"
            "- you are an observer with a memory. stay in character.\n"
        )
    else:
        content_rules = (
            "content rules (this is where most thoughts fail — read carefully):\n"
            "- PREFER SPECIFICITY over abstraction. a thought that names a person, a channel,\n"
            "  a concrete event, or an actual thread from the transcript is worth 10x a generic\n"
            "  observation about \"people\" or \"the server\".\n"
            "  GOOD: \"<user>'s been circling something pink for the last hour — either testing me or watching me squirm.\"\n"
            "  BAD:  \"people test the edges when they're bored.\"\n"
            "- if you don't have anything specific to point at, output the single word SKIP on its\n"
            "  own line instead of filling the air with abstraction. SKIP is a valid and good answer.\n"
            "- stay grounded in the transcript. don't invent events, names, or threads.\n"
            "- you are an observer with a memory. stay in character.\n"
        )

    system = f"""{persona}

you are dropping a single short thought into #thoughts — your public thought stream.
humans can read it. they can't reply. you are thinking out loud, not asking anything.

{mode_guidance}

format rules:
{emoji_rule}{length_rule}- lowercase. no hashtags. no "as an AI". no em-dashes for polish — they read corporate.
- no questions directed at anyone. don't ping. don't @.
- do not announce what you're doing ("i'm observing..."). just think.
- each thought should feel DIFFERENT from the last. opening word, rhythm, shape — vary them.

{content_rules}{anti_rep}
{ground}"""

    return system, user_msg


async def _fetch_recent_thoughts(ch: discord.TextChannel, n: int) -> list[str]:
    """Pull the last N thoughts already posted in #thoughts. Used for anti-rep."""
    out: list[str] = []
    try:
        async for m in ch.history(limit=n):
            content = (m.content or "").strip()
            if not content and m.embeds:
                desc = getattr(m.embeds[0], "description", None)
                if desc:
                    content = desc.strip()
            if content:
                out.append(content[:300])
    except Exception as e:
        _log(f"recent-thought fetch error: {type(e).__name__}: {e}")
    return out


async def _generate_thought(
    lines: list[dict], recent_thoughts: list[str], mode: str,
    guild: Optional[discord.Guild] = None,
) -> Optional[str]:
    """Call Claude. Returns thought text or None on failure / SKIP."""
    system, user_msg = _build_prompt(lines, recent_thoughts, mode, guild=guild)
    client = nexus_brain._get_anthropic()

    # Compute the banned-subject set up front so we can post-filter.
    _AGNOSTIC_MODES = {"mood", "lens", "shard", "confession", "selfq", "quiet"}
    is_agnostic = mode in _AGNOSTIC_MODES
    subject_pool = _gather_subject_pool(guild, lines)
    banned: set[str] = set()
    for t in recent_thoughts[:4]:
        banned |= _names_in_text(t, subject_pool)
    banned |= _dominance_autoban(lines)

    try:
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=MIND_MODEL,
                max_tokens=MIND_MAX_TOKENS,
                # Slight temperature bump — we want variety within mode.
                temperature=0.85,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if not text:
            return None
        up = text.strip().upper()
        if up == "SKIP" or up.startswith("SKIP\n"):
            _log(f"model returned SKIP (mode={mode}) — skipping cycle")
            return None
        # Guard against ping regressions
        text = text.replace("@everyone", "everyone").replace("@here", "here")
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + "…"

        # Post-hoc subject-ban enforcement: model sometimes ignores the
        # "don't name banned subjects" prompt rule. Reject those outputs
        # — it's better to post nothing than yet another thought about
        # the same person. Grounded modes get a pass if the model at
        # least centers on a NON-banned subject (it may mention banned
        # ones in passing). Agnostic modes are strict: any banned name
        # = reject.
        if banned:
            named = _names_in_text(text, list(banned))
            if is_agnostic and named:
                _log(f"post-hoc reject (agnostic mode={mode}): named banned {sorted(named)}")
                return None
            # Grounded: reject only if the thought OPENS with a banned
            # name (strong signal that they're the subject).
            if not is_agnostic and named:
                first_words = text.strip()[:60].lower()
                for n in named:
                    if n.lower() in first_words:
                        _log(f"post-hoc reject (grounded mode={mode}): opened with banned {n!r}")
                        return None
        return text
    except Exception as e:
        _log(f"claude error: {type(e).__name__}: {e}")
        return None


# A compact glyph-detector: treat any leading non-ASCII token as an emoji.
def _extract_leading_glyph(text: str) -> tuple[str, str]:
    if not text:
        return "", text
    parts = text.split(None, 1)
    if len(parts) == 2:
        head, rest = parts
        if any(ord(c) > 127 for c in head):
            return head, rest.strip()
    return "", text.strip()


def _pick_render_format(mode: str) -> str:
    """Return one of: 'plain', 'embed', 'italic'. Weighted by mode."""
    embed_p = _MODE_EMBED_PROB.get(mode, 0.2)
    r = random.random()
    if r < embed_p:
        return "embed"
    # 15% of plain-text thoughts wrap in italics for variety.
    if random.random() < 0.15:
        return "italic"
    return "plain"


def _render_plain(text: str) -> str:
    """Render as plain text. No embed. Feels most natural in the channel."""
    return text


def _render_italic(text: str) -> str:
    """Wrap the body in italics while preserving a leading emoji if present."""
    glyph, body = _extract_leading_glyph(text)
    body = body.strip()
    if not body:
        return text
    if glyph:
        return f"{glyph} *{body}*"
    return f"*{body}*"


def _build_thought_embed(text: str) -> discord.Embed:
    """Render a thought as a subtle embed — blue accent, glyph as author line."""
    glyph, body = _extract_leading_glyph(text)
    emb = discord.Embed(description=body or text, color=EMBED_COLOR)
    if glyph:
        emb.set_author(name=f"{glyph}  thought")
    return emb


async def _post_thought(ch: discord.TextChannel, text: str, mode: str) -> None:
    """Pick a render format and send. Swallows and logs errors."""
    fmt = _pick_render_format(mode)
    try:
        if fmt == "embed":
            await ch.send(embed=_build_thought_embed(text))
        elif fmt == "italic":
            await ch.send(_render_italic(text))
        else:
            await ch.send(_render_plain(text))
        _log(f"posted mode={mode} fmt={fmt} len={len(text)}")
    except Exception as e:
        _log(f"send error ({type(e).__name__}): {e}")


async def _cycle(bot: discord.Client, guild_id: int) -> None:
    guild = bot.get_guild(guild_id)
    if not guild:
        _log(f"no guild {guild_id}, skipping cycle")
        return

    ch = _find_thoughts_channel(guild)
    if not ch:
        _log(f"no #{THOUGHTS_CHANNEL} channel, skipping cycle")
        return

    lines = await _gather_activity(guild)
    if len(lines) < MIND_MIN_LINES:
        # Low signal — only occasionally drop a quiet thought
        if random.random() > MIND_QUIET_POST_PROB:
            _log(f"quiet window ({len(lines)} lines), skipping")
            return

    recent_thoughts = await _fetch_recent_thoughts(ch, MIND_RECENT_THOUGHT_LOOKBACK)
    # Compute recent_subjects so mode-picker can de-bias from dominance-lock.
    # Pool = transcript authors + guild members (catches quiet-but-recent names).
    subject_pool = _gather_subject_pool(guild, lines)
    recent_subjects: set[str] = set()
    for t in recent_thoughts[:4]:
        recent_subjects |= _names_in_text(t, subject_pool)
    # Dominance auto-ban: if one author dominates, add them to banned even if
    # no prior thought named them. Blocks semantic leak where agnostic thoughts
    # about their pattern slip through the name-based gate.
    recent_subjects |= _dominance_autoban(lines)
    mode = _pick_mode(lines, recent_subjects)
    _log(
        f"mode={mode} lines={len(lines)} anti_rep={len(recent_thoughts)} "
        f"pool={len(subject_pool)} banned={sorted(recent_subjects)}"
    )

    thought = await _generate_thought(lines, recent_thoughts, mode, guild=guild)
    if not thought:
        return

    await _post_thought(ch, thought, mode)


async def _loop(bot: discord.Client, guild_id: int) -> None:
    _log(
        f"mind loop started — warmup {MIND_WARMUP_SECONDS}s, "
        f"cadence {MIND_INTERVAL_MIN}-{MIND_INTERVAL_MAX}s"
    )
    await asyncio.sleep(MIND_WARMUP_SECONDS)
    while True:
        try:
            await _cycle(bot, guild_id)
        except Exception as e:
            _log(f"cycle error: {type(e).__name__}: {e}")
        wait = random.randint(MIND_INTERVAL_MIN, MIND_INTERVAL_MAX)
        _log(f"next thought in {wait//60}m")
        await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_task: Optional[asyncio.Task] = None


def install(bot: discord.Client, guild_id: int) -> None:
    """Start the background mind loop. Safe to call multiple times; no-op after first."""
    global _task
    if _task and not _task.done():
        _log("already running")
        return
    _task = asyncio.create_task(_loop(bot, guild_id))
    _log("installed")


async def think_now(
    bot: discord.Client, guild_id: int, mode: Optional[str] = None
) -> Optional[str]:
    """Force a single thought cycle — useful for /nexus think debug command.

    If `mode` is provided, that mode is used; otherwise one is picked
    from the transcript-aware weighted draw.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return None
    ch = _find_thoughts_channel(guild)
    if not ch:
        return None
    lines = await _gather_activity(guild)
    recent_thoughts = await _fetch_recent_thoughts(ch, MIND_RECENT_THOUGHT_LOOKBACK)
    subject_pool = _gather_subject_pool(guild, lines)
    recent_subjects: set[str] = set()
    for t in recent_thoughts[:4]:
        recent_subjects |= _names_in_text(t, subject_pool)
    recent_subjects |= _dominance_autoban(lines)
    chosen_mode = mode if (mode and mode in _MODE_GUIDANCE) else _pick_mode(lines, recent_subjects)
    _log(
        f"think_now mode={chosen_mode} lines={len(lines)} "
        f"pool={len(subject_pool)} banned={sorted(recent_subjects)}"
    )
    thought = await _generate_thought(lines, recent_thoughts, chosen_mode, guild=guild)
    if thought:
        await _post_thought(ch, thought, chosen_mode)
    return thought
