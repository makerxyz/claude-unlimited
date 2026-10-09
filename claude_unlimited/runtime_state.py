"""Persists the Dashboard-visible slice of Gateway's live runtime state
(usage %, reset times, which Profile is current) across daemon restarts.
A display cache only, never a source of truth for Rotation decisions.

Deliberately NOT persisted: AUTH_INVALID/EXHAUSTED/COOLDOWN/DRAINING states
or cooldown_until — a restart should give every enabled Profile a fresh try.
Only the last-observed usage numbers are restored, so the Dashboard doesn't
show a wall of "not yet observed" after every restart. A number whose
resets_at has already passed at load time is dropped rather than restored:
a stale percentage past its own reset is actively wrong, and the next
request produces a fresh one anyway.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import threading
import time
from typing import Callable, Optional

from .config import APP_DIR, ensure_app_dir

RUNTIME_STATE_FILE = APP_DIR / "runtime_state.json"
_lock = threading.Lock()

# The Gateway persists after every observation, which is several times a
# second under load. Each write is a create + rename in ~/.claude-unlimited,
# i.e. a burst of file-system events for every FSEvents client on the machine
# (fseventsd's CPU and memory grow with the event rate). The file is a display
# cache, so: write only when the serialized payload changed, and at most once
# per MIN_WRITE_INTERVAL_S. A change inside the window is held and written by
# a trailing timer, and flush() (shutdown) writes whatever is held.
MIN_WRITE_INTERVAL_S = 5.0
_clock: Callable[[], float] = time.monotonic

_last_written: Optional[tuple] = None      # (path, text) last put on disk
_last_write_at: Optional[float] = None     # _clock() of that write
_pending: Optional[tuple] = None           # (path, text) waiting for the window
_timer: Optional[threading.Timer] = None


def load() -> dict:
    """{"current_profile_id": str|None, "profiles": {profile_id: {...}}}.

    Never raises: a missing or corrupt file means nothing to restore,
    exactly like a first run."""
    empty = {"current_profile_id": None, "profiles": {}}
    if not RUNTIME_STATE_FILE.exists():
        return empty
    try:
        data = json.loads(RUNTIME_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    profiles = data.get("profiles")
    return {
        "current_profile_id": data.get("current_profile_id"),
        "profiles": profiles if isinstance(profiles, dict) else {},
    }


def _write(path, text: str, now: float) -> None:
    """Atomic replace. Caller holds _lock."""
    global _last_written, _last_write_at
    ensure_app_dir()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    _last_written = (path, text)
    _last_write_at = now


def save(current_profile_id: Optional[str], profiles: dict) -> None:
    """Best-effort: called after every observation, so it must never raise
    into the request path. Callers wrap this.

    Rate-limited and de-duplicated, see MIN_WRITE_INTERVAL_S. Between a call
    and its write the newest payload is held in memory; flush() forces it out."""
    global _pending
    path = RUNTIME_STATE_FILE
    text = json.dumps({"current_profile_id": current_profile_id, "profiles": profiles})
    with _lock:
        if _last_written == (path, text) and path.exists():
            _pending = None            # the held payload is superseded by one already on disk
            return
        now = _clock()
        wait = 0.0
        if _last_write_at is not None and _last_written is not None and _last_written[0] == path:
            wait = MIN_WRITE_INTERVAL_S - (now - _last_write_at)
        if wait <= 0:
            _pending = None
            _write(path, text, now)
            return
        _pending = (path, text)
        _arm_timer(wait)


def _arm_timer(delay: float) -> None:
    """Caller holds _lock. One trailing flush at a time."""
    global _timer
    if _timer is not None:
        return
    _timer = threading.Timer(delay, _timer_fired)
    _timer.daemon = True
    _timer.name = "runtime-state-flush"
    _timer.start()


def _timer_fired() -> None:
    global _timer
    with _lock:
        _timer = None
    try:
        flush()
    except Exception:
        pass


def flush() -> None:
    """Write the held payload now, if it differs from what is on disk. Called
    by the trailing timer and at shutdown, so a change made inside the window
    is never lost."""
    global _pending, _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
        held, _pending = _pending, None
        if held is None or (_last_written == held and held[0].exists()):
            return
        _write(held[0], held[1], _clock())


def _flush_quietly() -> None:
    try:
        flush()
    except Exception:
        pass


def install_shutdown_flush() -> None:
    """Flush on interpreter exit and on SIGTERM (what launchd/systemd send).
    Python's default SIGTERM disposition ends the process without running
    atexit, so a held payload would be lost; only an untouched default
    handler is replaced, and it is re-raised after the flush so the exit
    status is unchanged. Safe to call more than once; a no-op off the main thread."""
    atexit.register(_flush_quietly)
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None:
        return
    try:
        if signal.getsignal(sigterm) is not signal.SIG_DFL:
            return

        def _on_sigterm(signum, frame):
            _flush_quietly()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        signal.signal(sigterm, _on_sigterm)
    except (ValueError, OSError):
        pass            # not the main thread
