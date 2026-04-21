"""
Nexus lottery — the rare/surprise pillar.

Three features, install-time wired:

1) /fortune slash command
   Once per user per UTC day. Ephemeral. Short oracular line grounded in
   that user's recent messages. If already rolled today, returns what they
   got this morning + "come back tomorrow". Persisted in fortune_state.json.

2) Gold-border rare thoughts (~1%)
   Monkey-patches nexus_mind._post_thought at install() so ~1% of thought
   posts render as a gold-bordered embed with title "◇ rare ◇" and a
   different footer. The text is unchanged — only rendering.

3) Wake-up windows
   1-2 times a day at random, Nexus enters a 20-min "wake" where the mind
   loop's *next* cadence is shortened to 6-12min for ~3 cycles. Implemented
   by temporarily clamping nexus_mind.MIND_INTERVAL_MIN/MAX from a
   background task — pure attribute swap, no _loop wrap (cleaner + works
   with the bot's existing sleep).

Install signature:
    nexus_lottery.install(bot, guild_id)   # idempotent

Debug endpoint:
    POST /lottery  {"action": "wake"|"rare_preview"|"clear_fortunes"}
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import time
from pathlib import Path
from typing import Optional

import discord

import config
import nexus_brain
import nexus_debug_http
import nexus_mind


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
FORTUNE_MODEL = "claude-haiku-4-5-20251001"
FORTUNE_TEMPERATURE = 0.9
FORTUNE_MAX_TOKENS = 200

# Gold accent for the rare-thought embed.
RARE_COLOR = 0xE8B923
RARE_TITLE = "\u25c7 rare \u25c7"   # ◇ rare ◇
RARE_FOOTER = "a rare one"
RARE_CHANCE = 0.01  # ~1%

# Wake windows
WAKE_WINDOW_SECONDS = 20 * 60      # 20 min "hot" period
WAKE_INTERVAL_MIN = 6 * 60         # 6 min
WAKE_INTERVAL_MAX = 12 * 60        # 12 min
WAKE_PER_DAY_MIN = 1
WAKE_PER_DAY_MAX = 2
# Scheduler sleeps this long between "should I fire a wake?" checks.
WAKE_SCHEDULER_TICK_SECONDS = 15 * 60  # 15 min

# How much personal context to pull for a fortune
FORTUNE_LOOKBACK_HOURS = 72
FORTUNE_MSG_LIMIT = 40

STATE_PATH = Path(__file__).parent / "fortune_state.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[nexus_lottery] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State — fortune_state.json
# {user_id: {"date": "YYYY-MM-DD", "text": "..."}}
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"state load error: {type(e).__name__}: {e}")
    return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        _log(f"state save error: {type(e).__name__}: {e}")


def _today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Fortune generation
# ---------------------------------------------------------------------------
async def _gather_user_recent(
    guild: discord.Guild, user_id: int, hours: int, max_msgs: int
) -> list[str]:
    """Pull the user's own recent messages across listen channels."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    out: list[str] = []
    for ch in guild.text_channels:
        canon = config.canon_channel(ch.name)
        if canon in config.NEXUS_IGNORE_CHANNELS:
            continue
        if canon not in config.NEXUS_LISTEN_CHANNELS:
            continue
        try:
            async for msg in ch.history(limit=60, after=since, oldest_first=False):
                if msg.author.id != user_id:
                    continue
                content = (msg.content or "").strip()
                if not content or len(content) < 8:
                    continue
                if content.startswith("/") or content.startswith("!"):
                    continue
                out.append(content[:240])
                if len(out) >= max_msgs:
                    break
        except discord.Forbidden:
            continue
        except Exception as e:
            _log(f"user history read error in #{ch.name}: {type(e).__name__}: {e}")
            continue
        if len(out) >= max_msgs:
            break
    return out


def _build_fortune_prompt(user_name: str, recent: list[str]) -> tuple[str, str]:
    persona = nexus_brain._get_persona()
    if recent:
        transcript = "\n".join(f"- {line}" for line in recent[:FORTUNE_MSG_LIMIT])
        ground = (
            f"here is what {user_name} has been saying in the last "
            f"{FORTUNE_LOOKBACK_HOURS}h:\n\n{transcript}\n"
        )
    else:
        ground = (
            f"{user_name} hasn't said much in the last {FORTUNE_LOOKBACK_HOURS}h. "
            f"give them a fortune that doesn't pretend to know details you don't.\n"
        )

    system = f"""{persona}

you are giving {user_name} a single short daily fortune. one roll per day.
they'll see this as an ephemeral message — only they can read it.

shape:
- 1 to 2 sentences, max. 25 words or fewer total is ideal.
- oracular but grounded. specific, not generic.
- reference the texture of what they've been saying — a pattern, a tension,
  a thing they keep circling. not a literal quote.
- lowercase. no hashtags. no "as an AI". no em-dashes.
- no questions. no "good fortune awaits" platitudes. no horoscope cliches.
- do NOT address them by name. do NOT use second-person "dear reader".
- a line like "today you'll notice the thing you've been pretending isn't there"
  beats "luck favors you today." be that first one.

if you have nothing personal to work with, give them ONE small honest sentence
that's about the shape of a real day, not a fantasy. do not output SKIP. do not
output anything except the fortune itself.

{ground}"""
    user_msg = f"roll a fortune for {user_name}."
    return system, user_msg


async def _generate_fortune(user_name: str, recent: list[str]) -> Optional[str]:
    system, user_msg = _build_fortune_prompt(user_name, recent)
    try:
        client = nexus_brain._get_anthropic()
    except Exception as e:
        _log(f"anthropic client init error: {type(e).__name__}: {e}")
        return None
    try:
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=FORTUNE_MODEL,
                max_tokens=FORTUNE_MAX_TOKENS,
                temperature=FORTUNE_TEMPERATURE,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if not text:
            return None
        # Guard against pings
        text = text.replace("@everyone", "everyone").replace("@here", "here")
        # Trim to something sane
        if len(text) > 400:
            text = text[:400].rsplit(" ", 1)[0] + "\u2026"
        return text
    except Exception as e:
        _log(f"claude error (fortune): {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Slash command — /fortune
# ---------------------------------------------------------------------------
def _build_fortune_command(guild_id: int) -> discord.app_commands.Command:
    @discord.app_commands.command(
        name="fortune",
        description="roll your daily fortune (one per day, only you see it)",
    )
    async def fortune_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user = interaction.user
            user_id = str(user.id)
            today = _today_utc()
            state = _load_state()

            prior = state.get(user_id) or {}
            if prior.get("date") == today and prior.get("text"):
                msg = (
                    f"\u25c7 **your fortune for today**\n\n{prior['text']}\n\n"
                    f"*come back tomorrow.*"
                )
                await interaction.followup.send(msg, ephemeral=True)
                _log(f"/fortune repeat for {user.display_name} ({user_id})")
                return

            guild = interaction.guild
            recent: list[str] = []
            if guild is not None:
                try:
                    recent = await _gather_user_recent(
                        guild, user.id, FORTUNE_LOOKBACK_HOURS, FORTUNE_MSG_LIMIT
                    )
                except Exception as e:
                    _log(f"gather_user_recent error: {type(e).__name__}: {e}")

            text = await _generate_fortune(user.display_name, recent)
            if not text:
                await interaction.followup.send(
                    "*[the oracle is quiet right now — try again in a minute]*",
                    ephemeral=True,
                )
                return

            state[user_id] = {"date": today, "text": text}
            _save_state(state)

            body = f"\u25c7 **your fortune for today**\n\n{text}"
            await interaction.followup.send(body, ephemeral=True)
            _log(
                f"/fortune rolled for {user.display_name} ({user_id}) "
                f"recent_msgs={len(recent)} len={len(text)}"
            )
        except Exception as e:
            _log(f"/fortune error: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    f"*[fortune glitched: {type(e).__name__}]*",
                    ephemeral=True,
                )
            except Exception:
                pass

    return fortune_cmd


# ---------------------------------------------------------------------------
# Rare thought rendering — monkey-patch nexus_mind._post_thought
# ---------------------------------------------------------------------------
def _build_rare_embed(text: str) -> discord.Embed:
    """Render a rare thought as a gold-bordered embed with the rare title."""
    glyph, body = nexus_mind._extract_leading_glyph(text)
    desc = body if body else text
    emb = discord.Embed(title=RARE_TITLE, description=desc, color=RARE_COLOR)
    if glyph:
        emb.set_author(name=f"{glyph}  thought")
    emb.set_footer(text=RARE_FOOTER)
    return emb


def _install_rare_patch() -> None:
    """Wrap nexus_mind._post_thought so ~1% of posts render rare.

    Idempotent: won't double-wrap if already installed (checked via the
    _lottery_wrapped flag stamped on the wrapper).
    """
    original = getattr(nexus_mind, "_post_thought", None)
    if original is None:
        _log("rare patch skipped — nexus_mind._post_thought not found")
        return
    if getattr(original, "_lottery_wrapped", False):
        _log("rare patch already installed")
        return

    async def _post_thought_wrapped(
        ch: discord.TextChannel, text: str, mode: str
    ) -> None:
        try:
            if random.random() < RARE_CHANCE:
                try:
                    await ch.send(embed=_build_rare_embed(text))
                    _log(f"posted RARE mode={mode} len={len(text)}")
                    return
                except Exception as e:
                    _log(f"rare send error ({type(e).__name__}): {e} — falling back")
                    # Fall through to original on failure
        except Exception as e:
            _log(f"rare roll error ({type(e).__name__}): {e} — falling back")
        await original(ch, text, mode)

    _post_thought_wrapped._lottery_wrapped = True  # type: ignore[attr-defined]
    _post_thought_wrapped._lottery_original = original  # type: ignore[attr-defined]
    nexus_mind._post_thought = _post_thought_wrapped  # type: ignore[assignment]
    _log("rare patch installed (wraps nexus_mind._post_thought, ~1% gold)")


# ---------------------------------------------------------------------------
# Wake windows — temporary clamp of nexus_mind cadence
# ---------------------------------------------------------------------------
# We store original cadence so repeated wakes don't stack / lose the baseline.
_ORIGINAL_INTERVAL_MIN: Optional[int] = None
_ORIGINAL_INTERVAL_MAX: Optional[int] = None
_wake_task: Optional[asyncio.Task] = None
WAKE_UNTIL_TS: float = 0.0  # module-level: exposed via wake_active()


def wake_active() -> bool:
    """Return True if we're currently inside a wake window."""
    return time.time() < WAKE_UNTIL_TS


async def _run_wake_window(duration: float = WAKE_WINDOW_SECONDS) -> None:
    """Clamp nexus_mind's cadence for `duration` seconds, then restore."""
    global WAKE_UNTIL_TS, _ORIGINAL_INTERVAL_MIN, _ORIGINAL_INTERVAL_MAX
    # Snapshot current (non-wake) cadence the first time.
    if _ORIGINAL_INTERVAL_MIN is None:
        _ORIGINAL_INTERVAL_MIN = nexus_mind.MIND_INTERVAL_MIN
    if _ORIGINAL_INTERVAL_MAX is None:
        _ORIGINAL_INTERVAL_MAX = nexus_mind.MIND_INTERVAL_MAX

    WAKE_UNTIL_TS = time.time() + duration
    try:
        nexus_mind.MIND_INTERVAL_MIN = WAKE_INTERVAL_MIN
        nexus_mind.MIND_INTERVAL_MAX = WAKE_INTERVAL_MAX
        _log(
            f"WAKE start — cadence clamped to "
            f"{WAKE_INTERVAL_MIN//60}-{WAKE_INTERVAL_MAX//60}min "
            f"for {int(duration)//60}min"
        )
        await asyncio.sleep(duration)
    finally:
        # Restore baseline cadence even if the sleep was cancelled.
        try:
            nexus_mind.MIND_INTERVAL_MIN = _ORIGINAL_INTERVAL_MIN
            nexus_mind.MIND_INTERVAL_MAX = _ORIGINAL_INTERVAL_MAX
        except Exception as e:
            _log(f"wake restore error: {type(e).__name__}: {e}")
        WAKE_UNTIL_TS = 0.0
        _log("WAKE end — cadence restored")


async def _trigger_wake_once(duration: float = WAKE_WINDOW_SECONDS) -> None:
    """Kick off a wake window iff one isn't already running."""
    global _wake_task
    if _wake_task and not _wake_task.done():
        _log("wake trigger skipped — already in a wake window")
        return
    _wake_task = asyncio.create_task(_run_wake_window(duration))


async def _wake_scheduler() -> None:
    """Background loop: aim for WAKE_PER_DAY_MIN..MAX wakes per UTC day.

    Each tick we decide whether to fire based on how many wakes we've done
    today vs. the target and how much of the day remains. Cheap + stateless.
    """
    fire_count = 0
    current_day = _today_utc()
    daily_target = random.randint(WAKE_PER_DAY_MIN, WAKE_PER_DAY_MAX)
    _log(
        f"wake scheduler started — target {daily_target} wake(s)/day, "
        f"tick every {WAKE_SCHEDULER_TICK_SECONDS//60}min"
    )
    # Small warmup so we don't wake on boot
    await asyncio.sleep(10 * 60)

    while True:
        try:
            today = _today_utc()
            if today != current_day:
                current_day = today
                fire_count = 0
                daily_target = random.randint(WAKE_PER_DAY_MIN, WAKE_PER_DAY_MAX)
                _log(f"wake scheduler — new day {today}, target {daily_target}")

            if fire_count < daily_target and not wake_active():
                # Seconds remaining in this UTC day
                now_utc = dt.datetime.now(dt.timezone.utc)
                end_of_day = (now_utc + dt.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                remaining_s = max(1.0, (end_of_day - now_utc).total_seconds())
                remaining_wakes = daily_target - fire_count
                # Expected ticks left in the day
                ticks_left = max(1.0, remaining_s / WAKE_SCHEDULER_TICK_SECONDS)
                # Per-tick probability so expected fires ≈ remaining_wakes
                prob = min(1.0, remaining_wakes / ticks_left)
                if random.random() < prob:
                    await _trigger_wake_once()
                    fire_count += 1
        except Exception as e:
            _log(f"wake scheduler error: {type(e).__name__}: {e}")

        await asyncio.sleep(WAKE_SCHEDULER_TICK_SECONDS)


# ---------------------------------------------------------------------------
# Debug endpoint — POST /lottery
# ---------------------------------------------------------------------------
async def _handle_lottery(request):
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = str(body.get("action") or "").strip().lower()

    if action == "wake":
        await _trigger_wake_once()
        return web.json_response({
            "ok": True,
            "action": "wake",
            "wake_until_ts": WAKE_UNTIL_TS,
            "wake_active": wake_active(),
        })

    if action == "rare_preview":
        # Fire a rare-rendered thought into #thoughts right now (bypasses the
        # 1% roll — useful for visual QA).
        bot = getattr(nexus_debug_http, "_bot_ref", None)
        if bot is None or not getattr(bot, "guilds", None):
            return web.json_response(
                {"ok": False, "error": "bot not ready"}, status=503
            )
        guild = bot.guilds[0]
        ch = nexus_mind._find_thoughts_channel(guild)
        if not ch:
            return web.json_response(
                {"ok": False, "error": "no thoughts channel"}, status=503
            )
        sample = body.get("text") or "a rare one — preview from /lottery."
        try:
            await ch.send(embed=_build_rare_embed(str(sample)))
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"send failed: {type(e).__name__}: {e}"},
                status=500,
            )
        return web.json_response({"ok": True, "action": "rare_preview"})

    if action == "clear_fortunes":
        _save_state({})
        return web.json_response({"ok": True, "action": "clear_fortunes"})

    return web.json_response(
        {
            "ok": False,
            "error": "unknown action",
            "valid_actions": ["wake", "rare_preview", "clear_fortunes"],
            "wake_active": wake_active(),
            "wake_until_ts": WAKE_UNTIL_TS,
        },
        status=400,
    )


# ---------------------------------------------------------------------------
# Public install
# ---------------------------------------------------------------------------
_installed: bool = False


def install(bot: discord.Client, guild_id: int) -> None:
    """Install lottery features. Idempotent — safe to call multiple times."""
    global _installed
    if _installed:
        _log("already installed")
        return

    # 1) Slash command
    try:
        cmd = _build_fortune_command(guild_id)
        bot.tree.add_command(cmd, guild=discord.Object(id=guild_id))
        _log("registered /fortune slash command")
    except Exception as e:
        _log(f"slash register error: {type(e).__name__}: {e}")

    # 2) Rare thought monkey-patch
    try:
        _install_rare_patch()
    except Exception as e:
        _log(f"rare patch error: {type(e).__name__}: {e}")

    # 2b) Re-apply the rare patch whenever nexus_mind is hot-reloaded —
    #     importlib.reload(nexus_mind) re-creates _post_thought from source,
    #     blowing away our wrapper. The post-reload hook fixes that silently.
    try:
        nexus_debug_http.register_post_reload_hook(
            "nexus_mind", _install_rare_patch
        )
        _log("rare patch re-apply hook registered (fires on /reload nexus_mind)")
    except Exception as e:
        _log(f"post-reload hook register error: {type(e).__name__}: {e}")

    # 3) Wake-window scheduler (background task)
    try:
        global _wake_task  # noqa: PLW0603
        asyncio.create_task(_wake_scheduler())
        _log("wake scheduler task started")
    except Exception as e:
        _log(f"wake scheduler start error: {type(e).__name__}: {e}")

    # 4) Debug endpoint
    try:
        nexus_debug_http.register_route("POST", "/lottery", _handle_lottery)
        _log("debug endpoint registered (POST /lottery)")
    except Exception as e:
        _log(f"register_route error: {type(e).__name__}: {e}")

    _installed = True
    _log("installed")


__all__ = ["install", "wake_active", "WAKE_UNTIL_TS"]
