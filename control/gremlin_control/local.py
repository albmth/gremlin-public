"""Serve the API outside AWS against DynamoDB Local and an S3-compatible store.

Creates the table and bucket when missing, with the same key schema as the staging stack.
"""

import os

import uvicorn
from botocore.exceptions import ClientError
from pydantic import SecretStr

from gremlin_control.api import create_app
from gremlin_control.settings import Settings
from gremlin_control.store import ImprintStore


def provision(settings):
    store = ImprintStore(settings)
    client = store.table.client
    try:
        client.create_table(
            TableName=settings.table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        client.get_waiter("table_exists").wait(TableName=settings.table_name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise
    try:
        store.media.s3.create_bucket(
            Bucket=settings.media_bucket,
            CreateBucketConfiguration={"LocationConstraint": settings.region},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            raise


def main():
    settings = Settings()
    if settings.environment not in {"local", "test"}:
        raise SystemExit("The local server runs only in the local or test environment")
    if settings.local_worker:
        # The simulator stands in for the provider and the controller, at zero cost.
        settings = settings.model_copy(
            update={
                "controller_enabled": True,
                "imprint_hourly_microusd": settings.imprint_hourly_microusd or 1_580_000,
                "imprint_budget_microusd": settings.imprint_budget_microusd or 10**12,
                "imprint_rolling_budget_microusd": settings.imprint_rolling_budget_microusd
                or 10**12,
                "imprint_monthly_budget_microusd": settings.imprint_monthly_budget_microusd
                or 10**12,
                "imprint_endpoint_id": settings.imprint_endpoint_id or "simulated",
                "imprint_runpod_key": settings.imprint_runpod_key or SecretStr("simulated"),
            }
        )
    provision(settings)
    if settings.local_worker:
        from gremlin_control import simulator

        simulator.start(settings)
    host, port = os.environ.get("GREMLIN_LISTEN", "127.0.0.1:8000").rsplit(":", 1)
    uvicorn.run(create_app(settings), host=host, port=int(port), log_level="warning")


if __name__ == "__main__":
    main()
