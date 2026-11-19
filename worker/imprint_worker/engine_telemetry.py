"""Measure completed Comfy loader calls without changing its import or CUDA setup order.

These host wall times are subsets of tracking/generation, not extra stages to add to them.
File loading can return mapped tensor views; later page faults and asynchronous CUDA work
are not isolated by these counters. Device loading also includes residency/eviction checks.
No CUDA synchronization, model retention, function arguments or returned tensors are added.
"""

import functools
import importlib.abc
import importlib.machinery
import json
import math
import os
import runpy
import sys
import threading
import time
from pathlib import Path

FILENAME = "model-load-timings.json"
COUNTERS = ("model_file_load_seconds", "model_device_load_seconds")
_last_attempt_boot = None


class LoaderTimings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.values = {
            "version": 1,
            "booted_at_ns": time.time_ns(),
            **dict.fromkeys(COUNTERS, 0.0),
        }
        self._publish()

    def _publish(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.values, allow_nan=False))
        temporary.replace(self.path)

    def wrap(self, function, counter):
        @functools.wraps(function)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            result = function(*args, **kwargs)
            # A loader failure remains the original failure. Successful-call counters do
            # not pretend to measure partially completed loading on a failed attempt.
            elapsed = time.perf_counter() - started
            if not math.isfinite(elapsed) or elapsed < 0:
                raise RuntimeError("Invalid engine timing")
            with self.lock:
                self.values[counter] += elapsed
                self._publish()
            return result

        return measured


class _TimedImport(importlib.abc.MetaPathFinder):
    targets = {
        "comfy.utils": ("load_torch_file", "model_file_load_seconds"),
        "comfy.model_management": ("load_models_gpu", "model_device_load_seconds"),
    }

    def __init__(self, timings):
        self.timings = timings
        self.pending = set(self.targets)

    def find_spec(self, fullname, path, target=None):
        if fullname not in self.pending:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Pinned engine loader unavailable")
        original = spec.loader
        function, counter = self.targets[fullname]
        owner = self

        class Loader:
            def create_module(self, module_spec):
                return original.create_module(module_spec)

            def exec_module(self, module):
                original.exec_module(module)
                setattr(module, function, owner.timings.wrap(getattr(module, function), counter))
                owner.pending.remove(fullname)
                if not owner.pending:
                    sys.meta_path.remove(owner)

            def __getattr__(self, name):
                return getattr(original, name)

        spec.loader = Loader()
        return spec


def snapshot(engine_root):
    """Absent telemetry means an external fixture engine, not a zero-second load."""
    path = Path(engine_root) / "user" / FILENAME
    try:
        with path.open("rb") as stream:
            raw = stream.read(2049)
    except FileNotFoundError:
        return None
    try:
        values = json.loads(raw)
        if (
            len(raw) > 2048
            or set(values) != {"version", "booted_at_ns", *COUNTERS}
            or values["version"] != 1
            or type(values["booted_at_ns"]) is not int
            or values["booted_at_ns"] <= 0
            or any(
                type(values[key]) not in (int, float)
                or not math.isfinite(values[key])
                or values[key] < 0
                for key in COUNTERS
            )
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("Invalid engine timing receipt") from None
    return values


def begin_attempt(engine_root):
    global _last_attempt_boot
    before = snapshot(engine_root)
    if before is None:
        return None, {}
    first = int(before["booted_at_ns"] != _last_attempt_boot)
    _last_attempt_boot = before["booted_at_ns"]
    return before, {"engine_first_use": first}


def elapsed(engine_root, before):
    if before is None:
        return {}
    after = snapshot(engine_root)
    if after is None or after["booted_at_ns"] != before["booted_at_ns"]:
        raise RuntimeError("Engine timing receipt changed during attempt")
    delta = {key: after[key] - before[key] for key in COUNTERS}
    if any(value < 0 for value in delta.values()):
        raise RuntimeError("Engine timing counters moved backwards")
    return delta


def main():
    # Execute the unmodified pinned script. It configures device visibility and argument
    # parsing before importing torch; importing loader modules here would violate that.
    script = Path.cwd() / "main.py"
    sys.path.insert(0, str(script.parent))
    sys.argv[0] = str(script)
    timings = LoaderTimings(os.environ.pop("GREMLIN_ENGINE_TELEMETRY"))
    sys.meta_path.insert(0, _TimedImport(timings))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
