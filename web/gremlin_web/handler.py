"""AWS Lambda entry points. One package serves both functions; GREMLIN_WEB_ROLE picks the role.

The API function serves only its Function URL. The worker accepts the one-minute schedule,
asynchronous `process` requests from the API and operator actions from the deployer.
"""

import json
import logging

from mangum import Mangum

from gremlin_web.app import create_app
from gremlin_web.service import Service
from gremlin_web.settings import Settings

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("mangum").setLevel(logging.WARNING)


def _settings():
    settings = Settings()
    names = {
        "gremlin_token": settings.gremlin_token_parameter,
        "replicate_token": settings.replicate_token_parameter,
    }
    wanted = {field: name for field, name in names.items() if name}
    if not wanted:
        return settings
    import boto3

    # Read once per cold start; secrets never appear in configuration or logs. A missing
    # parameter raises, so the function fails closed, and the values are validated like any other.
    ssm = boto3.client("ssm", region_name=settings.region)
    values = {
        field: ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        for field, name in wanted.items()
    }
    return Settings(**values)


SETTINGS = _settings()
SERVICE = Service(SETTINGS)
_http = Mangum(create_app(SERVICE), lifespan="off") if SETTINGS.role == "api" else None


def handler(event, context):
    if SETTINGS.role == "api":
        if not isinstance(event, dict) or "http" not in event.get("requestContext", {}):
            raise ValueError("The API function serves only its Function URL")
        return _http(event, context)
    if SETTINGS.role != "worker":
        raise ValueError("Unsupported role")
    if event.get("web_operator"):
        return SERVICE.operate(event)
    if event.get("web") == "process":
        return {"outcome": SERVICE.generations.process(event["visitor"], event["gid"])}
    if event.get("source") == "aws.events":
        report = SERVICE.sweep()
        if SETTINGS.monitoring_enabled:
            # One embedded-metric record per sweep: missing samples alarm too.
            print(
                json.dumps(
                    {
                        "_aws": {
                            "Timestamp": int(SERVICE.clock() * 1000),
                            "CloudWatchMetrics": [
                                {
                                    "Namespace": "Gremlin/Web",
                                    "Dimensions": [["Environment"]],
                                    "Metrics": [{"Name": "Healthy", "Unit": "Count"}],
                                }
                            ],
                        },
                        "Environment": SETTINGS.environment,
                        "Healthy": 1 if report.get("healthy") else 0,
                    }
                )
            )
        return report
    raise ValueError("Unknown worker event: " + json.dumps(sorted(event)))
