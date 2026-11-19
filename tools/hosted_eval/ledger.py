"""Durable evidence and a single-process campaign lock."""

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path


class Hold(RuntimeError):
    """An unresolved safety or owner-review gate."""


def redact(value):
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k.lower() in {"authorization", "token", "api_key"} else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for name in ("REPLICATE_API_TOKEN", "FAL_KEY", "FAL_API_KEY"):
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"data:(?:image|video)/[^;]+;base64,[A-Za-z0-9+/=]+", "[input media]", value)
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as stream:
        json.dump(redact(value), stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class Ledger:
    def __init__(self, root):
        self.path = Path(root) / "ledger.jsonl"
        self.events = (
            [json.loads(line) for line in self.path.read_text().splitlines()]
            if self.path.exists()
            else []
        )

    def append(self, event, **fields):
        record = redact({"event": event, "at": time.time(), **fields})
        with self.path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(record)
        return record

    def select(self, event, cell=None):
        return [
            e
            for e in self.events
            if e["event"] == event and (cell is None or e.get("cell") == cell)
        ]


@contextmanager
def campaign_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Hold("Another campaign command holds the lock") from error
        yield
