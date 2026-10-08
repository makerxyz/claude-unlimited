"""The fork's upgrade tooling: the preflight markers must hold on the tree that
ships, and nothing in the update path may name the unmaintained upstream."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
posix_only = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),
                                reason="bash scripts")


@posix_only
def test_preflight_markers_hold_on_this_tree():
    result = subprocess.run([str(SCRIPTS / "preflight-patches.sh"), str(ROOT)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout


@posix_only
def test_preflight_fails_when_a_kept_hunk_is_missing(tmp_path):
    pkg = tmp_path / "claude_unlimited"
    shutil.copytree(ROOT / "claude_unlimited", pkg)
    gateway = pkg / "gateway.py"
    gateway.write_text(gateway.read_text().replace("_BILLING_HEADER_PREFIX", "_RENAMED"))
    result = subprocess.run([str(SCRIPTS / "preflight-patches.sh"), str(tmp_path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "MISSING" in result.stdout


@posix_only
@pytest.mark.parametrize("name", ["upgrade-when-idle.sh", "preflight-patches.sh"])
def test_scripts_parse(name):
    assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0


def test_nothing_that_fetches_or_installs_names_the_upstream():
    for rel in ("install.sh", "install.ps1", "claude_unlimited/updater.py",
                "claude_unlimited/hud.py", "scripts/upgrade-when-idle.sh"):
        assert "DevDock-AI" not in (ROOT / rel).read_text(), rel
