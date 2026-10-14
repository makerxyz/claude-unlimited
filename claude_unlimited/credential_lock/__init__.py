"""Cross-process transaction lock; lock files contain no credentials."""
import hashlib
import os
import threading
import time
from contextlib import contextmanager

from .. import config

if os.name == "nt":
    from .windows import acquire, release
else:
    from .posix import acquire, release

_guard = threading.Lock()
_threads = {}
_local = threading.local()


class LockUnavailable(RuntimeError):
    pass


@contextmanager
def locked(identity, timeout=120.0):
    # Same namespace for CLI and daemon. Hashes identify accounts, never tokens.
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _guard:
        mutex = _threads.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=timeout):
        raise LockUnavailable("Credential transaction is busy; retry later")
    try:
        held = getattr(_local, "held", set())
        if key in held:
            yield
            return
        _local.held = held
        directory = config.APP_DIR / "credential-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            deadline = time.monotonic() + timeout
            while not acquire(fd):
                if time.monotonic() >= deadline:
                    raise LockUnavailable("Credential transaction is busy; retry later")
                time.sleep(0.05)
            acquired = True
            held.add(key)
            yield
        finally:
            held.discard(key)
            try:
                if acquired:
                    release(fd)
            finally:
                os.close(fd)
    finally:
        mutex.release()
