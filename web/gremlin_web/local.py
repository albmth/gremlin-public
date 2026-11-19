"""Serve the website outside AWS against DynamoDB Local and an S3-compatible store.

The worker runs in-process: submissions are processed on a thread pool and a background loop
stands in for the one-minute schedule. Creates the table and bucket when missing.
"""

import contextlib
import json
import logging
import os

import uvicorn
from botocore.exceptions import ClientError

from gremlin_web.app import create_app
from gremlin_web.service import Service, run_local_sweeper
from gremlin_web.settings import Settings


# The browser uploads straight to the bucket, so the emulator needs CORS for the site.
def provision(service):
    settings = service.settings
    client = service.table.client
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
    s3 = service.media.s3
    try:
        s3.create_bucket(
            Bucket=settings.media_bucket,
            CreateBucketConfiguration={"LocationConstraint": settings.region},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            raise
    # MinIO applies CORS globally and refuses per-bucket rules.
    with contextlib.suppress(ClientError):
        s3.put_bucket_cors(
            Bucket=settings.media_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [settings.public_origin],
                        "AllowedMethods": ["PUT", "GET"],
                        "AllowedHeaders": ["content-type"],
                        "MaxAgeSeconds": 600,
                    }
                ]
            },
        )


def main():
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("gremlin_web").setLevel(logging.INFO)
    settings = Settings()
    if settings.environment != "local":
        raise SystemExit("The local server runs only in the local environment")
    host, port = os.environ.get("GREMLIN_WEB_LISTEN", "127.0.0.1:8080").rsplit(":", 1)
    service = Service(settings)
    provision(service)
    run_local_sweeper(service, float(os.environ.get("GREMLIN_WEB_SWEEP_SECONDS", "3")))
    print(json.dumps({"listening": f"http://{host}:{port}"}), flush=True)
    uvicorn.run(create_app(service), host=host, port=int(port), log_level="warning")


if __name__ == "__main__":
    main()
