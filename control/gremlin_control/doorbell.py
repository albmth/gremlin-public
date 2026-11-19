"""Wake the controller now instead of at the next one-minute sweep.

A ring is only a hint. The sweep reads the same durable state, so a lost ring delays work by
at most a minute and never loses it.
"""

import json
import logging

from gremlin_control.aws import _session

logger = logging.getLogger(__name__)
# Rings outside AWS are recorded for the contract suite instead of being delivered.
rings = []


def ring(settings):
    if settings.role != "api":
        rings.append(True)
        return
    try:
        _session().client("lambda", region_name=settings.region).invoke(
            FunctionName=settings.controller_function,
            InvocationType="Event",
            Payload=json.dumps({"gremlin": "doorbell"}).encode(),
        )
    except Exception:
        logger.error("gremlin.doorbell_unconfirmed")
