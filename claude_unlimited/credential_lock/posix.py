import fcntl


def acquire(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def release(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
