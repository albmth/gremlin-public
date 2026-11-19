"""The documented local development loop: the example CLI against the simulated worker.

The simulator publishes labelled synthetic media with the real worker's geometry, so a client
can exercise success, failure and delivery end to end with no GPU or provider.
"""

import hashlib
import json
import subprocess
import sys
import zipfile
from io import BytesIO

import pytest
import requests
from gremlin_contract.flows import INPUTS, intent
from gremlin_contract.harness import ROOT

TOKEN = "local-simulator-test-token"
pytestmark = pytest.mark.parametrize(
    "backend_settings", ["succeed", "reject", "fail"], indirect=True
)


@pytest.fixture
def backend_settings(request):
    return {
        "local_worker": request.param,
        "imprint_daily_jobs": "8",
        "client_token_hashes": json.dumps({"app": hashlib.sha256(TOKEN.encode()).hexdigest()}),
    }


def test_example_cli_reaches_the_simulated_outcome(api, backend_settings, tmp_path):
    (tmp_path / "token.json").write_text(json.dumps({"token": TOKEN}))
    request = {k: v for k, v in intent().items() if not k.endswith("_sha256")}
    (tmp_path / "request.json").write_text(json.dumps(request))
    for kind, data in INPUTS.items():
        (tmp_path / kind).write_bytes(data)
    run = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/gremlin_client.py"),
            "--base",
            api.url,
            "--token-file",
            str(tmp_path / "token.json"),
            "--request",
            str(tmp_path / "request.json"),
            "--reference",
            str(tmp_path / "reference"),
            "--driving",
            str(tmp_path / "driving"),
            "--output",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [json.loads(line) for line in run.stdout.splitlines()]
    stages = [line["stage"] for line in lines[:-1]]
    final = lines[-1]
    headers = {"Authorization": "Bearer " + TOKEN}
    job = requests.get(f"{api.url}/api/imprint/jobs/{final['job']}", headers=headers).json()
    diagnostics = requests.get(
        f"{api.url}/api/imprint/jobs/{final['job']}/diagnostics", headers=headers
    )
    record = json.loads(zipfile.ZipFile(BytesIO(diagnostics.content)).read("diagnostics.json"))
    if backend_settings["local_worker"] == "succeed":
        assert run.returncode == 0, run.stderr
        assert final["state"] == "succeeded" and final["outcome"]["kind"] == "ready"
        assert final["outcome"]["attempt_started"] is True
        stored = tmp_path / "out" / final["job"]
        files = job["result"]["files"]
        for name, entry in files.items():
            data = (stored / name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert (files["result.mp4"]["width"], files["result.mp4"]["frames"]) == (512, 55)
        assert files["result.webp"]["duration_ms"] == 3400
        # The client acknowledged durable storage, which shortens retention to 24 hours.
        assert job["media_expires_at"] - final["outcome"]["at"] <= 24 * 3600 + 5
        assert "failure_code" not in record
    else:
        code = {"reject": "input_rejected", "fail": "worker_attempt_failed"}
        code = code[backend_settings["local_worker"]]
        assert run.returncode == 1
        assert final["state"] == "failed"
        assert final["outcome"]["failure_code"] == code
        assert final["outcome"]["failure_reason"] == (
            "reference_subject_not_found" if code == "input_rejected" else None
        )
        assert job["result"] is None and record["failure_code"] == code
    assert {"queued", "running", "finishing"} & set(stages)
