"""The Lambda entry points route each event type to the right role, on the local emulators."""

import hashlib
import importlib
import json
import logging
import uuid

import pytest

TOKEN = "handler-test-token"


@pytest.fixture
def lambda_handler(monkeypatch):
    name = "gremlin-handler-" + uuid.uuid4().hex[:12]
    env = {
        "AWS_ACCESS_KEY_ID": "gremlinlocal",
        "AWS_SECRET_ACCESS_KEY": "gremlinlocalsecret",
        "GREMLIN_DYNAMODB_ENDPOINT": "http://127.0.0.1:8001",
        "GREMLIN_S3_ENDPOINT": "http://127.0.0.1:9100",
        "GREMLIN_TABLE_NAME": name,
        "GREMLIN_MEDIA_BUCKET": name,
        "GREMLIN_CLIENT_TOKEN_HASHES": json.dumps(
            {"c": hashlib.sha256(TOKEN.encode()).hexdigest()}
        ),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from gremlin_control import local, settings

    local.provision(settings.Settings())

    def load(role):
        monkeypatch.setenv("GREMLIN_ROLE", role)
        from gremlin_control import handler

        return importlib.reload(handler)

    return load


class Context:
    def get_remaining_time_in_millis(self):
        return 60_000


def url_event(method, path, body=None, token=TOKEN):
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "host": "example.lambda-url.eu-north-1.on.aws",
            "authorization": "Bearer " + token,
            "content-type": "application/json",
        },
        "requestContext": {
            "accountId": "anonymous",
            "apiId": "example",
            "domainName": "example.lambda-url.eu-north-1.on.aws",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "192.0.2.1",
                "userAgent": "test",
            },
            "requestId": str(uuid.uuid4()),
            "stage": "$default",
            "timeEpoch": 0,
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def test_api_function_serves_only_its_function_url(lambda_handler, caplog):
    caplog.set_level(logging.INFO)
    api = lambda_handler("api")
    health = api.handler(url_event("GET", "/health"), Context())
    assert health["statusCode"] == 200 and json.loads(health["body"])["status"] == "ok"
    intent = {
        "idempotency_key": "handler-request-0001",
        "reference_sha256": "a" * 64,
        "driving_sha256": "b" * 64,
        "reference_point": {"x": 0.5, "y": 0.5},
        "target_point": {"x": 0.5, "y": 0.5},
        "duration_ms": 3400,
        "prompt": "A person waves at the camera on a quiet street.",
        "tier": "simple-motion",
    }
    created = api.handler(url_event("POST", "/api/imprint/jobs", intent), Context())
    assert created["statusCode"] == 200 and json.loads(created["body"])["state"] == "uploading"
    refused = api.handler(url_event("GET", "/api/imprint/capabilities", token="wrong"), Context())
    assert refused["statusCode"] == 401
    callback_token = "fake-provider-callback-token"
    callback = api.handler(
        url_event("POST", f"/api/imprint/provider/{uuid.uuid4()}/{callback_token}"), Context()
    )
    assert callback["statusCode"] == 404
    assert callback_token not in caplog.text
    with pytest.raises(ValueError):
        api.handler({"gremlin_operator": "state"}, Context())


def test_controller_sweeps_and_answers_operator_actions(lambda_handler):
    controller = lambda_handler("controller")
    assert controller.handler({"source": "aws.events"}, Context()) == {"swept": True}
    assert controller.handler({"gremlin": "doorbell"}, Context()) == {"swept": True}
    state = controller.handler({"gremlin_operator": "state"}, Context())
    assert state["sessions"] == [] and state["dispatch_holds"] == []
    assert state["controller_enabled"] is False and state["budget_held_microusd"] == 0
    with pytest.raises(ValueError, match="authorize_recovery"):
        controller.handler(
            {"gremlin_operator": "release_dispatch_hold", "session_id": str(uuid.uuid4())},
            Context(),
        )
    unknown = controller.handler(
        {
            "gremlin_operator": "release_dispatch_hold",
            "session_id": str(uuid.uuid4()),
            "authorize_recovery": True,
        },
        Context(),
    )
    assert unknown["error"]["code"] == "IMPRINT_SESSION_ACTIVE"
    with pytest.raises(ValueError):
        controller.handler(url_event("GET", "/health"), Context())
