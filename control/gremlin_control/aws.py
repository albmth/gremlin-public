"""DynamoDB and S3 access: one table, optimistic transactions, presigned media transfers."""

from __future__ import annotations

import base64
import random
import time
from decimal import Decimal
from functools import cache

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

# Epoch-second attributes; every other number is an integer.
FLOATS = {
    "created_at",
    "requested_at",
    "completed_at",
    "updated_at",
    "expires_at",
    "terminal_at",
    "deadline",
    "finished_at",
    "lease_expires",
    "started_at",
    "url_until",
    "due_at",
    "moderation_until",
    # Result geometry: an MP4 of 55 frames at 16 fps lasts 3437.5 ms.
    "duration_ms",
}


class Conflict(Exception):
    """Another writer changed an item this transaction read; re-read and decide again."""


def to_dynamo(value):
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value, name=None):
    if isinstance(value, Decimal):
        return float(value) if name in FLOATS else int(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, set):
        return set(value)
    return value


@cache
def _session():
    return boto3.session.Session()


def _client(service, settings, **extra):
    kwargs = {"region_name": settings.region, "config": Config(**extra)}
    endpoint = settings.dynamodb_endpoint if service == "dynamodb" else settings.s3_endpoint
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return kwargs


class Table:
    def __init__(self, settings):
        resource = _session().resource(
            "dynamodb", **_client("dynamodb", settings, retries={"mode": "standard"})
        )
        self.table = resource.Table(settings.table_name)
        self.client = resource.meta.client
        self.name = settings.table_name

    def get(self, pk, sk):
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return from_dynamo(item) if item else None

    def query(self, pk, *, before=None, after=None, limit=None, forward=True):
        condition = Key("pk").eq(pk)
        if before is not None:
            condition &= Key("sk").lte(before)
        if after is not None:
            condition &= Key("sk").gt(after)
        kwargs = {
            "KeyConditionExpression": condition,
            "ConsistentRead": True,
            "ScanIndexForward": forward,
        }
        items = []
        while True:
            if limit is not None:
                kwargs["Limit"] = limit - len(items)
            page = self.table.query(**kwargs)
            items += [from_dynamo(item) for item in page["Items"]]
            if "LastEvaluatedKey" not in page or (limit is not None and len(items) >= limit):
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def count(self, pk, after_sk):
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").gt(after_sk),
            "ConsistentRead": True,
            "Select": "COUNT",
        }
        total = 0
        while True:
            page = self.table.query(**kwargs)
            total += page["Count"]
            if "LastEvaluatedKey" not in page:
                return total
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def scan(self):
        kwargs = {"ConsistentRead": True}
        while True:
            page = self.table.scan(**kwargs)
            yield from (from_dynamo(item) for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put(self, item):
        self.table.put_item(Item=to_dynamo(item))


class Txn:
    """One TransactWriteItems call; versioned items carry `v` and are replaced whole."""

    def __init__(self, table):
        self.table = table
        self.items = []
        self.keys = set()

    def _add(self, kind, body, key):
        if key in self.keys:
            raise AssertionError(f"Transaction touches {key} twice")
        self.keys.add(key)
        self.items.append({kind: {"TableName": self.table.name, **body}})

    def put(self, item):
        """Replace a versioned item if nobody wrote it since it was read; a new item has no `v`."""
        item = dict(item)
        version = item.get("v")
        if version is None:
            body = {"ConditionExpression": "attribute_not_exists(pk)"}
        else:
            body = {
                "ConditionExpression": "#v = :v",
                "ExpressionAttributeNames": {"#v": "v"},
                "ExpressionAttributeValues": {":v": version},
            }
        item["v"] = (version or 0) + 1
        self._add("Put", {"Item": to_dynamo(item), **body}, (item["pk"], item["sk"]))
        return item

    def put_index(self, item):
        """Index entries (queue, due, admissions) are guarded by their owner's version."""
        self._add("Put", {"Item": to_dynamo(item)}, (item["pk"], item["sk"]))

    def delete(self, pk, sk, version=None, *, condition=None, values=None):
        body = {"Key": {"pk": pk, "sk": sk}}
        if version is not None:
            body["ConditionExpression"] = "#v = :v"
            body["ExpressionAttributeNames"] = {"#v": "v"}
            body["ExpressionAttributeValues"] = {":v": version}
        elif condition:
            body["ConditionExpression"] = condition
            body["ExpressionAttributeValues"] = to_dynamo(values or {})
        self._add("Delete", body, (pk, sk))

    def check(self, pk, sk, version):
        if version is None:
            body = {"ConditionExpression": "attribute_not_exists(pk)"}
        else:
            body = {
                "ConditionExpression": "#v = :v",
                "ExpressionAttributeNames": {"#v": "v"},
                "ExpressionAttributeValues": {":v": version},
            }
        self._add("ConditionCheck", {"Key": {"pk": pk, "sk": sk}, **body}, (pk, sk))

    def commit(self):
        if not self.items:
            return
        try:
            self.table.client.transact_write_items(TransactItems=self.items)
        except ClientError as error:
            if error.response["Error"]["Code"] in {
                "TransactionCanceledException",
                "TransactionConflictException",
            }:
                raise Conflict from error
            raise


def optimistic(table, decide, attempts=10):
    """Run `decide(txn)` on fresh reads until its transaction commits unchanged."""
    for attempt in range(attempts):
        txn = Txn(table)
        try:
            result = decide(txn)
            txn.commit()
            return result
        except Conflict:
            time.sleep(random.uniform(0, 0.02 * (attempt + 1)))
    raise RuntimeError("Persistent write contention")


class Media:
    """Private objects; clients and workers move bytes with short-lived presigned URLs."""

    def __init__(self, settings):
        self.bucket = settings.media_bucket
        self.s3 = _session().client(
            "s3",
            **_client(
                "s3",
                settings,
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_endpoint else "virtual"},
                retries={"mode": "standard"},
            ),
        )

    def presign_put(self, key, sha256, size, seconds):
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": size,
                "ChecksumSHA256": checksum,
            },
            ExpiresIn=seconds,
        )
        # Both headers are signed: storage refuses any other length or content.
        return url, {"Content-Length": str(size), "x-amz-checksum-sha256": checksum}

    def presign_get(self, key, seconds, content_type):
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseCacheControl": "no-store",
                "ResponseContentType": content_type,
            },
            ExpiresIn=seconds,
        )

    def head(self, key):
        """Return (bytes, sha256 hex) for an object stored with a SHA-256 checksum, else None."""
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        checksum = head.get("ChecksumSHA256")
        if not checksum or "-" in checksum:
            return head["ContentLength"], None
        return head["ContentLength"], base64.b64decode(checksum).hex()

    def get(self, key, *, limit=None):
        try:
            with self.s3.get_object(Bucket=self.bucket, Key=key)["Body"] as stream:
                data = stream.read() if limit is None else stream.read(limit + 1)
                if limit is not None and len(data) > limit:
                    raise ValueError("Stored media exceeds its byte limit")
                return data
        except ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def put(self, key, data, sha256):
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
        )

    def keys(self, prefix):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            yield from (item["Key"] for item in page.get("Contents", ()))

    def delete(self, keys):
        keys = list(keys)
        for start in range(0, len(keys), 1000):
            batch = [{"Key": key} for key in keys[start : start + 1000]]
            result = self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
            )
            if result.get("Errors"):
                # Keep the record retryable; the next sweep deletes again.
                raise OSError("Media deletion unconfirmed")
