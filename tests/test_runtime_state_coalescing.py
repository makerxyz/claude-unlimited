"""runtime_state.save() is called after every gateway observation (~4/s under
load). Each write is a create + rename in ~/.claude-unlimited, which is a burst
of file-system events for every FSEvents client on the machine, so writes are
de-duplicated and coalesced to one per MIN_WRITE_INTERVAL_S, with a flush so
nothing is lost at shutdown."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import types

import pytest

from claude_unlimited import runtime_state


@pytest.fixture
def clock(monkeypatch, tmp_path):
    state_file = tmp_path / "runtime_state.json"
    monkeypatch.setattr(runtime_state, "RUNTIME_STATE_FILE", state_file)
    monkeypatch.setattr(runtime_state, "MIN_WRITE_INTERVAL_S", 5.0)
    now = [1000.0]
    monkeypatch.setattr(runtime_state, "_clock", lambda: now[0])
    # The trailing timer is real-time; keep it from firing mid-test.
    armed = []
    monkeypatch.setattr(runtime_state, "_arm_timer", lambda delay: armed.append(delay))
    writes = []
    real = runtime_state._write
    monkeypatch.setattr(runtime_state, "_write", lambda p, t, n: (writes.append(t), real(p, t, n))[1])
    return types.SimpleNamespace(path=state_file, armed=armed, writes=writes, now=now)


def _on_disk(path):
    return json.loads(path.read_text())["profiles"]["a"]["last_usage_percent"]


def test_identical_payloads_are_written_once(clock):
    for _ in range(50):
        runtime_state.save("a", {"a": {"last_usage_percent": 10}})
    assert len(clock.writes) == 1
    assert _on_disk(clock.path) == 10


def test_changes_inside_the_window_are_held_not_written(clock):
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    for pct in range(2, 40):
        clock.now[0] += 0.25                      # 4 observations a second, 9.5 s in all
        runtime_state.save("a", {"a": {"last_usage_percent": pct}})
    # the leading write, then one when the 5 s window opened (pct 21 at t=5.0)
    assert len(clock.writes) == 2
    assert _on_disk(clock.path) == 21


def test_at_most_one_write_per_interval_at_four_observations_a_second(clock):
    seconds = 60
    for i in range(seconds * 4):
        clock.now[0] = 1000.0 + i * 0.25
        runtime_state.save("a", {"a": {"last_usage_percent": i}})
    # 60 s / 5 s = 12 windows, plus the leading write: far below the 240 calls.
    assert len(clock.writes) <= 13, len(clock.writes)
    assert len(clock.writes) >= 10


def test_flush_writes_the_held_payload(clock):
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    clock.now[0] += 1
    runtime_state.save("a", {"a": {"last_usage_percent": 99}})
    assert _on_disk(clock.path) == 1                   # held
    assert clock.armed                            # and a trailing flush is scheduled
    runtime_state.flush()
    assert _on_disk(clock.path) == 99                  # nothing lost
    assert len(clock.writes) == 2
    runtime_state.flush()                         # idempotent
    assert len(clock.writes) == 2


def test_a_change_back_to_the_written_value_cancels_the_held_write(clock):
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    clock.now[0] += 1
    runtime_state.save("a", {"a": {"last_usage_percent": 2}})
    clock.now[0] += 1
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    runtime_state.flush()
    assert len(clock.writes) == 1


def test_a_deleted_file_is_rewritten_even_if_the_payload_is_unchanged(clock):
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    clock.path.unlink()
    clock.now[0] += 6                             # past the window
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    assert clock.path.exists()


def test_the_trailing_timer_really_flushes(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_state, "RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr(runtime_state, "MIN_WRITE_INTERVAL_S", 0.3)
    runtime_state.save("a", {"a": {"last_usage_percent": 1}})
    runtime_state.save("a", {"a": {"last_usage_percent": 2}})
    assert _on_disk(tmp_path / "runtime_state.json") == 1
    done = threading.Event()
    deadline = 3.0
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline:
        if _on_disk(tmp_path / "runtime_state.json") == 2:
            done.set()
            break
        time.sleep(0.05)
    assert done.is_set(), "trailing flush never wrote the held payload"


def test_load_round_trips(clock):
    runtime_state.save("a", {"a": {"last_usage_percent": 7}})
    assert runtime_state.load()["current_profile_id"] == "a"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_flushes_a_held_write(tmp_path):
    """The daemon is stopped with SIGTERM, whose default disposition skips
    atexit. A change held inside the window must still reach disk."""
    state = tmp_path / "runtime_state.json"
    script = textwrap.dedent(f"""
        import pathlib, sys, time
        from claude_unlimited import runtime_state
        runtime_state.RUNTIME_STATE_FILE = pathlib.Path({str(state)!r})
        runtime_state.MIN_WRITE_INTERVAL_S = 3600.0
        runtime_state.install_shutdown_flush()
        runtime_state.save("a", {{"a": {{"last_usage_percent": 1}}}})
        runtime_state.save("a", {{"a": {{"last_usage_percent": 42}}}})
        print("ready", flush=True)
        time.sleep(60)
    """)
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert _on_disk(state) == 1
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == -signal.SIGTERM                  # still dies of SIGTERM
    assert _on_disk(state) == 42
