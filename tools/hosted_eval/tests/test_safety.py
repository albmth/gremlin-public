"""Campaign risks: duplicate spend, leaked keys, untrusted downloads and schema drift."""

import json
from pathlib import Path

import pytest

from tools.hosted_eval.budget import Budget
from tools.hosted_eval.ledger import Hold, Ledger, campaign_lock, write_json
from tools.hosted_eval.matrix import CELLS, MODELS, STILLS
from tools.hosted_eval.providers import Client, allowed, data_uri, validate
from tools.hosted_eval.run import confirm_probe, execute, probe_gate


@pytest.fixture
def campaign(tmp_path):
    ledger = Ledger(tmp_path)
    Budget(tmp_path, ledger).approve("2.00")
    return tmp_path, ledger


def test_reservations_survive_restart_and_failure(campaign):
    root, ledger = campaign
    ledger.append("intent", cell="x", attempt="a", reserve_usd="1.95")
    ledger.append("terminal", cell="x", attempt="a", status="failed", metric_usd=None)
    ledger = Ledger(root)
    budget = Budget(root, ledger)
    budget.reserve("0.05")
    with pytest.raises(Hold, match="ceiling"):
        budget.reserve("0.050001")
    ledger.append("posted", attempt="a", posted_usd="1.99")
    with pytest.raises(Hold, match="ceiling"):
        budget.reserve("0.02")


@pytest.mark.parametrize("amount", ["2.01", "0", "-1", "NaN", "Infinity"])
def test_hard_cap_cannot_be_raised(campaign, amount):
    root, ledger = campaign
    with pytest.raises(Hold):
        Budget(root, ledger).approve(amount)


def test_approval_required(tmp_path):
    with pytest.raises(Hold, match="approve"):
        Budget(tmp_path, Ledger(tmp_path)).reserve(".01")


def test_probe_requires_actual_evidence_and_stops_mismatch(campaign):
    root, ledger = campaign
    ledger.append(
        "intent", cell="KF-A0", model="V1", attempt="a", reserve_usd=".0125", list_usd=".0125"
    )
    ledger.append("terminal", cell="KF-A0", attempt="a", status="succeeded")
    with pytest.raises(Hold, match="posted charge"):
        probe_gate(ledger, "V1")
    evidence = root / "provider-billing.txt"
    evidence.write_text("prediction a: USD .025")
    with pytest.raises(Hold, match="10%"):
        confirm_probe(root, "V1", ".025", evidence)
    with pytest.raises(Hold):
        probe_gate(Ledger(root), "V1")
    assert Budget(root, Ledger(root)).total() == __import__("decimal").Decimal("0.025")
    confirm_probe(root, "V1", ".025", evidence, accept_deviation=True)
    probe_gate(Ledger(root), "V1")
    assert Ledger(root).select("probe")[-1]["within_tolerance"] is False


def test_redaction_covers_files_and_provider_echoes(campaign, monkeypatch):
    root, ledger = campaign
    tokens = ["replicate-fake-secret", "fal-fake-secret", "fal-alias-secret"]
    for name, token in zip(["REPLICATE_API_TOKEN", "FAL_KEY", "FAL_API_KEY"], tokens, strict=True):
        monkeypatch.setenv(name, token)
    value = {
        "error": " ".join(tokens),
        "authorization": "Bearer unknown",
        "image": "data:image/png;base64,QUJD",
    }
    ledger.append("intent", cell="redaction", body=value)
    write_json(root / "response.json", value)
    for path in root.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert all(token not in text for token in tokens)
            assert "Bearer unknown" not in text
            assert "QUJD" not in text


@pytest.mark.parametrize(
    "url",
    [
        "http://replicate.delivery/a",
        "https://replicate.delivery.evil.test/a",
        "https://evil.test/replicate.delivery",
        "https://user:pass@replicate.delivery/a",
        "https://replicate.delivery:999/a",
    ],
)
def test_download_host_fence(url):
    with pytest.raises(Hold):
        allowed(url, ["replicate.delivery"])


def test_no_transport_retry_and_no_auth_redirect(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        raise ConnectionError("ambiguous response")

    client = Client("replicate", "fake", "test/model")
    monkeypatch.setattr(client.session, "request", request)
    with pytest.raises(ConnectionError):
        client.submit({"prompt": "x"})
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False


def test_schema_drift_blocks_unsupported_fields_and_enum():
    schema = {
        "type": "object",
        "required": ["resolution"],
        "properties": {"resolution": {"type": "string", "enum": ["480p"]}},
    }
    for body in ({"resolution": "720p"}, {"resolution": "480p", "optimize_prompt": False}, {}):
        with pytest.raises(Hold):
            validate(body, schema, {})


def test_uncertain_intent_never_reposts(campaign, monkeypatch):
    root, ledger = campaign
    ledger.append("intent", cell="KF-A0", attempt="ambiguous", model="V1", reserve_usd=".0125")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake")
    monkeypatch.setattr("tools.hosted_eval.run.verify", lambda root: {})
    monkeypatch.setattr("tools.hosted_eval.run.build", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(Client, "submit", lambda *a: pytest.fail("duplicate paid POST"))
    with pytest.raises(Hold, match="never re-POST"):
        execute(root, "KF-A0", "V1", "kungfu")


def test_concurrent_commands_cannot_submit(campaign):
    root, _ = campaign
    with campaign_lock(root), pytest.raises(Hold, match="lock"), campaign_lock(root):
        pytest.fail("second lock acquired")


def test_matrix_is_priced_and_contains_only_phase_a():
    assert len(CELLS) == 9 and len(STILLS) == 4
    assert {c["model"] for c in CELLS} <= MODELS.keys()
    assert all(MODELS[c["model"]]["reserve_usd"] > 0 for c in CELLS)


def test_input_size_rejected_before_request(tmp_path):
    path = tmp_path / "large.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1_000_000)
    with pytest.raises(Hold, match="under 1 MB"):
        data_uri(path)


def test_truncated_ledger_fails_closed(tmp_path):
    (tmp_path / "ledger.jsonl").write_text('{"event": "intent"')
    with pytest.raises(json.JSONDecodeError):
        Ledger(tmp_path)


def test_page_has_no_eager_key_script():
    page = Path("tools/hosted_eval/page.html").read_text()
    assert '<script src="key.js"' not in page
    assert "if(!complete(f))return" in page


def test_explicit_rejection_gets_one_fresh_reserved_attempt(campaign, monkeypatch):
    from tools.hosted_eval.providers import HTTPFailure

    root, ledger = campaign
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake")
    monkeypatch.setattr("tools.hosted_eval.run.verify", lambda root: {})
    monkeypatch.setattr("tools.hosted_eval.run.build", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr("tools.hosted_eval.run.time.sleep", lambda _: None)
    monkeypatch.setattr(Client, "schema", lambda _: ({}, {}, {}))
    count = []

    def submit(*args):
        count.append(1)
        raise HTTPFailure("replicate", "POST", 429)

    monkeypatch.setattr(Client, "submit", submit)
    with pytest.raises(Hold, match="single provider-failure retry"):
        execute(root, "KF-A0", "V1", "kungfu")
    ledger = Ledger(root)
    assert len(count) == 2
    intents = ledger.select("intent")
    assert intents[0]["attempt"] != intents[1]["attempt"]
    assert str(Budget(root, ledger).total()) == "0.0250"


def test_report_preserves_binary_review_without_numeric_scores_or_reveal(tmp_path):
    from tools.hosted_eval.report import report

    ledger = Ledger(tmp_path)
    ledger.append("letters", fixture="kungfu", mapping={"hidden-model-cell": "A"})
    snapshot = {
        "run": tmp_path.name,
        "scores": {
            "kungfu:A": {"ratings": {}, "sticker": "yes", "notes": "Keep motion | needs work"}
        },
        "revealed": {},
    }
    path = tmp_path / "scores.json"
    write_json(path, snapshot)
    report(tmp_path, path)
    text = (tmp_path / "report.md").read_text()
    assert "| A | yes | not scored |" in text
    assert "Keep motion \\| needs work" in text
    assert "hidden-model-cell" not in text
    assert json.loads(path.read_text()) == snapshot
    assert "Owner review recorded" in text
