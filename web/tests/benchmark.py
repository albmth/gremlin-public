"""Owner media stays untracked. Labels travel; absent media is an explicit skipped check."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LABELS = json.loads((ROOT / "tests/fixtures/generation-recovery.json").read_text())


def path(side):
    label = LABELS[side]
    source = ROOT / label["path"]
    if not source.is_file():
        pytest.skip(f"Owner benchmark unavailable: {source.name}")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == label["sha256"]
    return source
