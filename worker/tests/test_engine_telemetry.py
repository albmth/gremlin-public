"""Loader timing must preserve startup order, values, failures and engine reuse."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from imprint_worker import engine_telemetry as telemetry
from imprint_worker.lifecycle import Runtime


def test_wrapped_loaders_preserve_values_errors_and_exclude_arguments(tmp_path, monkeypatch):
    path = tmp_path / telemetry.FILENAME
    timings = telemetry.LoaderTimings(path)
    ticks = iter([10, 12.5, 13])
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: next(ticks))
    result = object()

    def load(path, *, metadata=False):
        assert path == "/private/model.safetensors" and metadata
        return result

    measured = timings.wrap(load, "model_file_load_seconds")
    assert measured("/private/model.safetensors", metadata=True) is result
    error = ValueError("private failing model path")

    def broken():
        raise error

    with pytest.raises(ValueError) as caught:
        timings.wrap(broken, "model_device_load_seconds")()
    assert caught.value is error
    receipt = json.loads(path.read_text())
    assert receipt["model_file_load_seconds"] == 2.5
    assert receipt["model_device_load_seconds"] == 0
    assert all(type(value) in (int, float) for value in receipt.values())
    assert "private" not in path.read_text()


def test_attempt_deltas_distinguish_first_use_and_reused_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_last_attempt_boot", None)
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        root = runtime.engine
        timings = telemetry.LoaderTimings(root / "user" / telemetry.FILENAME)
        before, flags = telemetry.begin_attempt(root)
        assert flags == {"engine_first_use": 1}
        timings.values["model_file_load_seconds"] = 2.5
        timings.values["model_device_load_seconds"] = 4.0
        timings._publish()
        assert telemetry.elapsed(root, before) == {
            "model_file_load_seconds": 2.5,
            "model_device_load_seconds": 4.0,
        }
        runtime.clear_media()
        before, flags = telemetry.begin_attempt(root)
        assert flags == {"engine_first_use": 0}
        assert telemetry.elapsed(root, before) == dict.fromkeys(telemetry.COUNTERS, 0)
        timings.values["booted_at_ns"] += 1
        timings._publish()
        with pytest.raises(RuntimeError, match="changed during attempt"):
            telemetry.elapsed(root, before)
        assert telemetry.begin_attempt(root)[1] == {"engine_first_use": 1}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "secret"])
def test_invalid_or_absent_telemetry_never_becomes_zero_load_evidence(tmp_path, value):
    assert telemetry.begin_attempt(tmp_path) == (None, {})
    (tmp_path / "user").mkdir()
    path = tmp_path / "user" / telemetry.FILENAME
    timings = telemetry.LoaderTimings(path)
    values = timings.values | {"model_file_load_seconds": value}
    path.write_text(json.dumps(values))
    with pytest.raises(RuntimeError, match="Invalid engine timing receipt"):
        telemetry.snapshot(tmp_path)


def test_launcher_observes_natural_imports_after_device_setup(tmp_path):
    """An eager import would initialize torch before the engine selects its device."""
    (tmp_path / "comfy").mkdir()
    (tmp_path / "comfy" / "__init__.py").write_text("")
    (tmp_path / "torch.py").write_text(
        "import os\nassert os.environ['DEVICE_SETUP_COMPLETE'] == 'yes'\n"
    )
    (tmp_path / "comfy" / "utils.py").write_text(
        "import torch\n"
        "def load_torch_file(path, *, return_metadata=False):\n"
        "    return (path, return_metadata)\n"
    )
    (tmp_path / "comfy" / "model_management.py").write_text(
        "import torch\ndef load_models_gpu(models, **kwargs):\n    return models\n"
    )
    (tmp_path / "main.py").write_text(
        "import os, sys\n"
        "assert 'torch' not in sys.modules\n"
        "assert sys.argv[1:] == ['--fast-disk', '--offline']\n"
        "os.environ['DEVICE_SETUP_COMPLETE'] = 'yes'\n"
        "import comfy.utils, comfy.model_management\n"
        "assert comfy.utils.load_torch_file('/private/weights', return_metadata=True) "
        "== ('/private/weights', True)\n"
        "models = object()\n"
        "assert comfy.model_management.load_models_gpu(models) is models\n"
        "assert not any(type(f).__name__ == '_TimedImport' for f in sys.meta_path)\n"
    )
    path = tmp_path / telemetry.FILENAME
    env = os.environ | {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "GREMLIN_ENGINE_TELEMETRY": str(path),
    }
    subprocess.run(
        [sys.executable, "-m", "imprint_worker.engine_telemetry", "--fast-disk", "--offline"],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=10,
    )
    receipt = json.loads(path.read_text())
    assert all(receipt[key] > 0 for key in telemetry.COUNTERS)
    assert "private" not in path.read_text()
