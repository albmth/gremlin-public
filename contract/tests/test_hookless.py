"""Checks safe to run against a deployed origin: no hooks, no spending, one client token.

Staging has no GPU spending authority, so a submitted job stops at the admission check.
"""

import hashlib
import uuid

import pytest
from gremlin_contract.flows import INPUTS, intent

pytestmark = pytest.mark.hookless


def test_authentication_validation_and_closed_admission(api, headers):
    assert api.get("/health").json() == {"service": "gremlin", "status": "ok"}
    assert api.get("/api/imprint/capabilities").json()["detail"]["code"] == "CLIENT_AUTH_REQUIRED"
    wrong = api.get("/api/imprint/capabilities", headers={"Authorization": "Bearer wrong"})
    assert wrong.json()["detail"]["code"] == "CLIENT_AUTH_INVALID"
    capabilities = api.get("/api/imprint/capabilities", headers=headers).json()
    assert capabilities["tiers"] == ["simple-motion", "complex-motion"]
    assert capabilities["contract_version"] == 3
    # A closed deployment says why, so clients never guess from a failed submit.
    admission = capabilities["admission"]
    assert admission["accepting"] is False and admission["dispatching"] is False
    assert admission["reason_code"] in {
        "spending_unauthorized",
        "endpoint_unconfigured",
        "daily_limit_reached",
    }
    invalid = api.post("/api/imprint/jobs", json=intent(tier="automatic"), headers=headers)
    assert invalid.status_code == 422 and isinstance(invalid.json()["detail"], list)
    assert api.get("/no-such-route").status_code == 404


def test_create_upload_submit_cancel_without_spending(api, headers):
    request = intent(idempotency_key="hookless-" + uuid.uuid4().hex)
    created = api.post("/api/imprint/jobs", json=request, headers=headers)
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]
    try:
        assert api.post("/api/imprint/jobs", json=request, headers=headers).json()["id"] == job_id
        for kind, data in INPUTS.items():
            stored = api.upload_input(job_id, kind, data, headers)
            assert stored.ok, stored
            assert stored.body == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        refused = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers)
        assert refused.status_code == 503
        assert refused.json()["detail"]["code"] == "IMPRINT_BUDGET_BLOCKED"
        assert refused.json()["detail"]["reason"] in {
            "spending_unauthorized",
            "endpoint_unconfigured",
        }
    finally:
        cancelled = api.delete(f"/api/imprint/jobs/{job_id}", headers=headers)
    assert cancelled.json()["state"] == "cancelled"
    assert (
        api.get(f"/api/imprint/jobs/{job_id}/results/result.mp4", headers=headers).status_code
        == 404
    )
