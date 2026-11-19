"""Exclusive ownership and restart cleanup for the serverless worker's disposable files."""

import fcntl
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def deadline(seconds, code):
    """Bound native code and stalled transfers as well as Python/HTTP timeouts."""

    def expire():
        # The production handler is PID 1. Its exit ends the container and paid computation;
        # an exception in a timer thread cannot interrupt a blocked decoder/native kernel.
        try:
            os.write(2, (json.dumps({"error": code}, separators=(",", ":")) + "\n").encode())
        finally:
            os._exit(1)

    watchdog = threading.Timer(seconds, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        watchdog.cancel()


def remove(path):
    """Unlink symlinks rather than following them outside the owned scratch directory."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


class Runtime:
    def __init__(self, root="/work/imprint-runtime"):
        self.root = Path(root)
        self.jobs = self.root / "jobs"
        self.engine = self.root / "engine"
        self.lock = None

    def __enter__(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.root.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        self.lock = os.fdopen(descriptor, "w")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Worker scratch is still owned by another process") from None
        return self

    def __exit__(self, *_):
        # The engine inherits this descriptor. Closing only the parent's descriptor leaves
        # the lock held if an engine survived a crashed parent, fencing a new cleanup.
        self.lock.close()

    def clear(self):
        """Reset all disposable state only before engine start or after engine termination."""
        if self.lock is None or self.lock.closed:
            raise RuntimeError("Worker scratch cleanup requires exclusive ownership")
        remove(self.root)
        for path in (
            self.jobs,
            *(self.engine / name for name in ("input", "output", "temp", "user")),
            self.root / "cache",
        ):
            path.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(path, 10001, 10001)

    def clear_media(self):
        """The caller holds the handler lock and has confirmed that the engine is idle."""
        if self.lock is None or self.lock.closed:
            raise RuntimeError("Worker scratch cleanup requires exclusive ownership")
        for path in (self.jobs, *(self.engine / name for name in ("input", "output", "temp"))):
            remove(path)
            path.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(path, 10001, 10001)
