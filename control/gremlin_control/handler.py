"""AWS Lambda entry points. One package serves both functions; GREMLIN_ROLE picks the role.

The API function accepts only Function URL events. The controller accepts the scheduled sweep,
doorbells and operator actions, which only principals allowed to invoke it can send.
"""

import json
import logging
import os

from fastapi import HTTPException
from mangum import Mangum

from gremlin_control import controller
from gremlin_control.api import create_app
from gremlin_control.settings import Settings

logging.getLogger().setLevel(logging.INFO)
# Mangum logs raw request paths, including the provider callback's bearer capability.
logging.getLogger("mangum.http").setLevel(logging.WARNING)


def _settings():
    settings = Settings()
    if settings.role == "controller" and settings.imprint_runpod_key_parameter:
        import boto3
        from pydantic import SecretStr

        # Read once per cold start; the key never appears in configuration or logs.
        parameter = boto3.client("ssm", region_name=settings.region).get_parameter(
            Name=settings.imprint_runpod_key_parameter, WithDecryption=True
        )
        settings.imprint_runpod_key = SecretStr(parameter["Parameter"]["Value"])
    return settings


SETTINGS = _settings()
_http = Mangum(create_app(SETTINGS), lifespan="off") if SETTINGS.role == "api" else None


def handler(event, context):
    if SETTINGS.role == "api":
        if not isinstance(event, dict) or "http" not in event.get("requestContext", {}):
            raise ValueError("The API function serves only its Function URL")
        return _http(event, context)
    if SETTINGS.role != "controller":
        raise ValueError(f"Unsupported role {os.environ.get('GREMLIN_ROLE')}")
    if event.get("gremlin_operator"):
        try:
            return controller.operate(SETTINGS, event)
        except HTTPException as error:
            return {"error": error.detail}
    if event.get("gremlin") == "doorbell" or event.get("source") == "aws.events":
        controller.run(SETTINGS, time_left=lambda: context.get_remaining_time_in_millis() / 1000)
        return {"swept": True}
    raise ValueError("Unknown controller event: " + json.dumps(sorted(event)))
