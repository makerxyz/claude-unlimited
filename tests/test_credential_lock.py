import os
import subprocess
import sys
import textwrap

import pytest

from claude_unlimited.credential_lock import LockUnavailable, locked


def test_another_process_cannot_enter_until_transaction_commits(tmp_path, monkeypatch):
    import claude_unlimited.config as config
    monkeypatch.setattr(config, 'APP_DIR', tmp_path)
    program = textwrap.dedent('''
        import sys
        from pathlib import Path
        import claude_unlimited.config as config
        from claude_unlimited.credential_lock import locked, LockUnavailable
        config.APP_DIR = Path(sys.argv[1])
        try:
            with locked('profile:a', timeout=0.15):
                print('entered')
        except LockUnavailable:
            print('busy')
    ''')
    with locked('profile:a'):
        result = subprocess.run([sys.executable, '-c', program, str(tmp_path)], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'busy'
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path)], capture_output=True, text=True, timeout=5)
    assert result.stdout.strip() == 'entered'


def test_nested_transaction_does_not_deadlock():
    with locked('profile:a', timeout=0.1):
        with locked('profile:a', timeout=0.1):
            pass


def test_release_after_exception_allows_next_transaction():
    with pytest.raises(ValueError):
        with locked('profile:a'):
            raise ValueError('fixture')
    with locked('profile:a', timeout=0.1):
        pass
