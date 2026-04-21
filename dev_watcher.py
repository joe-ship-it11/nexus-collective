"""dev_watcher.py — auto-restart nexus_bot.py on any .py save.

Watches the current directory (non-recursive) for .py changes using watchdog.
When a relevant file is saved, kills any running nexus_bot.py processes via
psutil, waits 1s, and relaunches `python nexus_bot.py` with stdout/stderr
appended to nexus_bot.log.

Saves that arrive within 1.5s of each other are debounced into a single
restart triggered by the last save.

Usage:
    python dev_watcher.py            # run the watcher
    python dev_watcher.py --dry-run  # import check, print "ok", exit 0

This module has no side effects on import.
"""
from __future__ import annotations

import os
import sys
import time
import threading
import subprocess
from pathlib import Path
from datetime import datetime

# --- config ------------------------------------------------------------------
BOT_SCRIPT = "nexus_bot.py"
LOG_FILE = "nexus_bot.log"
DEBOUNCE_SECONDS = 1.5
KILL_GRACE_SECONDS = 1.0

# Files/patterns we never want to trigger on.
IGNORE_EXACT = {
    "dev_watcher.py",
    "DISPATCH_PROMPT.md",
    "voice_transcripts.jsonl",
}

IGNORE_PREFIXES = (
    "nexus_bot.log",      # the log file itself and any rotations
    "debug_whisper",      # debug_whisper*.py
    "test_",              # test_*.py
)

IGNORE_DIR_PARTS = {"__pycache__"}


def _is_ignored(path: Path) -> bool:
    """Return True if this path should NOT trigger a restart."""
    name = path.name
    if name in IGNORE_EXACT:
        return True
    for prefix in IGNORE_PREFIXES:
        if name.startswith(prefix):
            return True
    # Skip anything inside __pycache__ / similar.
    for part in path.parts:
        if part in IGNORE_DIR_PARTS:
            return True
    return False


# --- bot process management --------------------------------------------------
def _kill_bot_procs() -> int:
    """Kill every python process whose command line references nexus_bot.py.

    Returns the number of processes killed.
    """
    import psutil  # local import so --dry-run stays fast & safe

    me = os.getpid()
    killed = 0
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not cmdline:
                continue
            joined = " ".join(cmdline)
            # match on path-independent script name
            if BOT_SCRIPT in joined and "dev_watcher.py" not in joined:
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Wait briefly for graceful exit, then hard-kill stragglers.
    if killed:
        gone, alive = psutil.wait_procs(
            [p for p in psutil.process_iter() if _cmd_has_bot(p)],
            timeout=KILL_GRACE_SECONDS,
        )
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return killed


def _cmd_has_bot(proc) -> bool:
    try:
        cmdline = proc.cmdline()
    except Exception:
        return False
    joined = " ".join(cmdline or [])
    return BOT_SCRIPT in joined and "dev_watcher.py" not in joined


def _launch_bot(here: Path):
    """Launch nexus_bot.py detached, appending output to nexus_bot.log."""
    log_path = here / LOG_FILE
    log_f = open(log_path, "ab")
    # On Windows, start a new process group so Ctrl-C in the watcher doesn't
    # slam the child before we explicitly kill it.
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(
        [sys.executable, BOT_SCRIPT],
        cwd=str(here),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


# --- debounced restart engine ------------------------------------------------
class Restarter:
    """Debounces file-save events and drives one nexus_bot.py child process."""

    def __init__(self, here: Path):
        self.here = here
        self._lock = threading.Lock()
        self._pending_trigger: str | None = None
        self._timer: threading.Timer | None = None
        self._restart_count = 0
        self._child: subprocess.Popen | None = None

    # --- event intake --------------------------------------------------------
    def schedule(self, trigger_file: str):
        with self._lock:
            self._pending_trigger = trigger_file
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            trigger = self._pending_trigger or "(unknown)"
            self._pending_trigger = None
            self._timer = None
        self._do_restart(trigger)

    # --- child lifecycle -----------------------------------------------------
    def start_initial(self):
        """Boot nexus_bot.py once at watcher startup."""
        self._restart_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"[dev_watcher] restart #{self._restart_count} at {ts} — trigger: <startup>",
            flush=True,
        )
        _kill_bot_procs()  # clear any stragglers from a previous session
        time.sleep(KILL_GRACE_SECONDS)
        self._child = _launch_bot(self.here)

    def _do_restart(self, trigger: str):
        self._restart_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"[dev_watcher] restart #{self._restart_count} at {ts} — trigger: {trigger}",
            flush=True,
        )
        # Check for a crash on the previous child and surface it.
        if self._child is not None:
            rc = self._child.poll()
            if rc is not None and rc != 0:
                print(
                    f"[dev_watcher] previous nexus_bot.py exited with code {rc}",
                    flush=True,
                )
        killed = _kill_bot_procs()
        if killed:
            print(f"[dev_watcher] killed {killed} running bot proc(s)", flush=True)
        time.sleep(KILL_GRACE_SECONDS)
        try:
            self._child = _launch_bot(self.here)
        except Exception as e:
            # Do not crash-loop — just report and wait for next save.
            print(f"[dev_watcher] launch failed: {e!r}", flush=True)
            self._child = None

    def poll_child(self):
        """Call periodically: surface unexpected child crashes (no auto-relaunch)."""
        if self._child is None:
            return
        rc = self._child.poll()
        if rc is None:
            return
        if rc != 0:
            print(
                f"[dev_watcher] nexus_bot.py crashed (exit {rc}) — waiting for next save",
                flush=True,
            )
        # Either way, stop tracking until we relaunch on next event.
        self._child = None

    def shutdown(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        _kill_bot_procs()


# --- watchdog glue -----------------------------------------------------------
def _make_handler(restarter: "Restarter", here: Path):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def _maybe_trigger(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            # Non-recursive: only react to files directly in `here`.
            try:
                if p.parent.resolve() != here.resolve():
                    return
            except Exception:
                return
            if p.suffix != ".py":
                return
            if _is_ignored(p):
                return
            restarter.schedule(p.name)

        def on_modified(self, event):
            self._maybe_trigger(event)

        def on_created(self, event):
            self._maybe_trigger(event)

        def on_moved(self, event):
            # Editors often save via atomic rename: react to dest path.
            if getattr(event, "dest_path", None):
                class _Fake:
                    is_directory = event.is_directory
                    src_path = event.dest_path
                self._maybe_trigger(_Fake())

    return _Handler()


def main(argv: list[str]) -> int:
    if "--dry-run" in argv:
        print("ok")
        return 0

    here = Path(__file__).resolve().parent
    from watchdog.observers import Observer

    restarter = Restarter(here)
    handler = _make_handler(restarter, here)
    observer = Observer()
    observer.schedule(handler, str(here), recursive=False)
    observer.start()

    print(f"[dev_watcher] watching {here} (non-recursive, *.py)", flush=True)
    restarter.start_initial()

    try:
        while True:
            time.sleep(1.0)
            restarter.poll_child()
    except KeyboardInterrupt:
        print("[dev_watcher] shutting down", flush=True)
    finally:
        observer.stop()
        observer.join(timeout=3.0)
        restarter.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
