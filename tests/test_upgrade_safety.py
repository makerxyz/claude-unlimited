import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upgrade_guard", ROOT / "scripts/upgrade_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize("snapshot,expected", [
    ({"status": "ok", "serving_now": [], "idle_seconds": 10}, "idle"),
    ({"status": "ok", "serving_now": [], "idle_seconds": None}, "idle"),
    ({"status": "ok", "serving_now": ["a"], "idle_seconds": 100}, "busy"),
    ({"status": "ok", "serving_now": [], "idle_seconds": 0}, "busy"),
    ({"status": "ok"}, "unknown"),
    ({"status": "ok", "serving_now": None, "idle_seconds": 20}, "unknown"),
    ({"status": "ok", "serving_now": [], "idle_seconds": True}, "unknown"),
    ({"status": "ok", "serving_now": [], "idle_seconds": float("nan")}, "unknown"),
    ({"status": "ok", "serving_now": [], "idle_seconds": -1}, "unknown"),
    ([], "unknown"),
])
def test_status_requires_positive_idle_evidence(snapshot, expected):
    assert guard.classify_status(json.dumps(snapshot), 10) == expected


@pytest.mark.parametrize("raw", ["", "timeout", "{", "null"])
def test_failed_status_is_unknown(raw):
    assert guard.classify_status(raw, 10) == "unknown"


@pytest.mark.parametrize("raw,force,expected", [
    ("", False, 1),  # both status and health can time out with live requests
    ('{"status":"ok","serving_now":["a"],"idle_seconds":0}', False, 1),
    ('{"status":"ok","serving_now":[],"idle_seconds":20}', False, 0),
    ("", True, 0),
])
def test_upgrade_gate_aborts_before_swap_on_busy_or_unreachable(tmp_path, raw, force, expected):
    source = (ROOT / "scripts/upgrade-when-idle.sh").read_text()
    # Exercise the actual pre-swap gate, without building or touching an install.
    gate = source[source.index('case "$MAX_WAIT"'):source.index("# ---- 4.")]
    stage = tmp_path / "stage"
    scratch = tmp_path / "scratch"
    stage.mkdir()
    scratch.mkdir()
    script = '''set -uo pipefail
log() { printf '%s\\n' "$*"; }
curl() { printf '%s' "$TEST_STATUS"; }
''' + gate + '\nprintf "SWAP_ALLOWED\\n"\n'
    import os
    env = dict(os.environ, MAX_WAIT="0", IDLE_SECONDS="10", PY=sys.executable,
               HERE=str(ROOT / "scripts"), BASE="http://unused", STAGE=str(stage),
               W=str(scratch), TEST_STATUS=raw, CU_FORCE_RESTART="1" if force else "0")
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == expected
    assert ("SWAP_ALLOWED" in result.stdout) == (expected == 0)
    if expected:
        assert "live install left unchanged" in result.stdout


def test_upgrade_preflight_rejects_a_package_that_restores_background(tmp_path):
    package = tmp_path / "claude_unlimited"
    shutil.copytree(ROOT / "claude_unlimited", package, ignore=shutil.ignore_patterns("__pycache__"))
    script = ROOT / "scripts/preflight-patches.sh"
    before = subprocess.run(["bash", str(script), str(package)], capture_output=True, text=True)
    assert before.returncode == 0, before.stdout
    installer = package / "daemon_installer/macos_launchd.py"
    installer.write_text(installer.read_text().replace('"ProcessType": "Interactive"', '"ProcessType": "Background"'))
    rejected = subprocess.run(["bash", str(script), str(package)], capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "background launchd service" in rejected.stdout
