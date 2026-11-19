"""DynamoDB and S3 access: one table, optimistic versioned transactions, presigned transfers.

The workload permission boundary grants PutItem, DeleteItem and ConditionCheckItem but not
UpdateItem, so every change replaces a whole item guarded by its version `v`.
"""

from __future__ import annotations

import random
import time
from decimal import Decimal
from functools import cache

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError


class Conflict(Exception):
    """Another writer changed an item this transaction read; re-read and decide again."""


def to_dynamo(value):
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


@cache
def _session():
    return boto3.session.Session()


def _kwargs(settings, endpoint, **config):
    kwargs = {"region_name": settings.region, "config": Config(**config)}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return kwargs


class Table:
    def __init__(self, settings):
        resource = _session().resource(
            "dynamodb",
            **_kwargs(settings, settings.dynamodb_endpoint, retries={"mode": "standard"}),
        )
        self.table = resource.Table(settings.table_name)
        self.client = resource.meta.client
        self.name = settings.table_name

    def get(self, pk, sk):
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return from_dynamo(item) if item else None

    def query(self, pk, *, limit=None, forward=True, after=None):
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(pk),
            "ConsistentRead": True,
            "ScanIndexForward": forward,
        }
        if after:
            kwargs["ExclusiveStartKey"] = {"pk": pk, "sk": after}
        items = []
        while True:
            if limit is not None:
                kwargs["Limit"] = limit - len(items)
            page = self.table.query(**kwargs)
            items += [from_dynamo(item) for item in page["Items"]]
            if "LastEvaluatedKey" not in page or (limit is not None and len(items) >= limit):
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put_new(self, item):
        """Write an item that must not exist yet (versioned from 1)."""
        try:
            self.table.put_item(
                Item=to_dynamo(item | {"v": 1}), ConditionExpression="attribute_not_exists(pk)"
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise Conflict from error
            raise


class Txn:
    """One TransactWriteItems call; versioned items carry `v` and are replaced whole."""

    def __init__(self, table):
        self.table = table
        self.items = []
        self.keys = set()

    def _add(self, kind, body, key):
        if key in self.keys:
            raise AssertionError(f"Transaction touches {key} twice")
        if len(self.items) >= 100:
            raise AssertionError("Transaction exceeds 100 items")
        self.keys.add(key)
        self.items.append({kind: {"TableName": self.table.name, **body}})

    def put(self, item):
        """Write a new item (no `v`) or replace one nobody changed since it was read."""
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

    def delete(self, item):
        body = {"Key": {"pk": item["pk"], "sk": item["sk"]}}
        if item.get("v") is not None:
            body["ConditionExpression"] = "#v = :v"
            body["ExpressionAttributeNames"] = {"#v": "v"}
            body["ExpressionAttributeValues"] = {":v": item["v"]}
        self._add("Delete", body, (item["pk"], item["sk"]))

    def commit(self):
        if not self.items:
            return
        try:
            self.table.client.transact_write_items(TransactItems=self.items)
        except ClientError as error:
            if error.response["Error"]["Code"] in {
                "TransactionCanceledException",
                "TransactionConflictException",
                "ConditionalCheckFailedException",
            }:
                raise Conflict from error
            raise


def optimistic(table, decide, attempts=12):
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
    """The site's bucket: raw uploads, normalized inputs and delivered results, all private."""

    def __init__(self, settings):
        self.bucket = settings.media_bucket
        self.s3 = _session().client(
            "s3",
            **_kwargs(
                settings,
                settings.s3_endpoint,
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_endpoint else "virtual"},
                retries={"mode": "standard"},
            ),
        )

    def presign_put(self, key, size, content_type, seconds=900):
        url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": size,
                "ContentType": content_type,
            },
            ExpiresIn=seconds,
        )
        # Both headers are signed: storage refuses another length or type.
        return {"method": "PUT", "url": url, "headers": {"Content-Type": content_type}}

    def presign_get(self, key, seconds, content_type, filename=None):
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ResponseCacheControl": "no-store",
            "ResponseContentType": content_type,
        }
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=seconds)

    def size(self, key):
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def download(self, key, path):
        self.s3.download_file(self.bucket, key, str(path))

    def get(self, key):
        try:
            with self.s3.get_object(Bucket=self.bucket, Key=key)["Body"] as body:
                return body.read()
        except ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def put(self, key, data, content_type):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def delete_prefix(self, prefix):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": item["Key"]} for item in page.get("Contents", ())]
            if keys:
                result = self.s3.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True}
                )
                if result.get("Errors"):
                    raise OSError("Media deletion unconfirmed")
