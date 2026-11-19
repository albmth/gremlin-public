"""Black-box unavailable moderation fails closed and ends the client's reservation."""

from gremlin_contract.flows import create, upload_inputs


def test_moderation_outage_is_bounded_and_never_starts_an_attempt(api, backend, headers, runpod):
    # Both SDK services point at the existing local fake, which does not implement them.
    # Its 404 responses exercise real SDK failure handling without contacting AWS.
    backend.env["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"] = runpod.url
    backend.env["AWS_ENDPOINT_URL_REKOGNITION"] = runpod.url
    backend.restart()
    api.reapply()
    api.configure_provider()
    assert (
        "safety_moderation"
        not in api.get("/api/imprint/capabilities", headers=headers).json()["features"]
    )
    api.settings(
        moderation_enabled=True, moderation_guardrail_id="test", moderation_guardrail_version="1"
    )
    assert (
        "safety_moderation"
        in api.get("/api/imprint/capabilities", headers=headers).json()["features"]
    )
    job_id = create(api, headers)["id"]
    upload_inputs(api, job_id, headers)
    for _ in range(3):
        response = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "IMPRINT_MODERATION_UNAVAILABLE"
    job = api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()
    assert job["outcome"]["kind"] == "failed"
    assert job["outcome"]["failure_code"] == "moderation_unavailable"
    assert job["outcome"]["attempt_started"] is False
    assert api.records()["attempts"] == []
    assert api.records()["sessions"] == []
    assert create(api, headers, idempotency_key="moderation-recovery-0002")["state"] == "uploading"
