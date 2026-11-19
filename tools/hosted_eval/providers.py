"""Plain HTTP clients with explicit hosts, timeouts and no implicit POST retries."""

import base64
import time
from urllib.parse import urlparse

import requests

from .ledger import Hold


class HTTPFailure(Hold):
    def __init__(self, provider, method, status):
        self.status = status
        super().__init__(f"{provider} {method} HTTP {status}")


MAX_BYTES = 100 * 1024 * 1024


def allowed(url, hosts):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise Hold("Provider URL is not permitted")
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise Hold("Provider URL host is not permitted")
    return url


def data_uri(path):
    data = path.read_bytes()
    if len(data) >= 1_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise Hold("Inputs must be PNG files under 1 MB")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def download(url, path, hosts):
    """No auth headers, and every redirect is independently host checked."""
    begin = time.time()
    for _ in range(5):
        allowed(url, hosts)
        with requests.get(url, stream=True, timeout=(15, 60), allow_redirects=False) as response:
            if response.is_redirect:
                url = response.headers["Location"]
                continue
            if response.status_code != 200:
                raise Hold(f"Download HTTP {response.status_code}")
            if int(response.headers.get("Content-Length", 0)) > MAX_BYTES:
                raise Hold("Download exceeds 100 MB")
            size, first = 0, None
            part = path.with_suffix(path.suffix + ".part")
            with part.open("wb") as stream:
                for block in response.iter_content(65536):
                    first = first or time.time()
                    size += len(block)
                    if size > MAX_BYTES:
                        raise Hold("Download exceeds 100 MB")
                    stream.write(block)
            if not size:
                raise Hold("Empty output")
            part.replace(path)
            return {
                "download_first_byte": first,
                "download_last_byte": time.time(),
                "download_s": time.time() - begin,
                "bytes": size,
            }
    raise Hold("Too many download redirects")


def validate(value, schema, schemas):
    """Validate provider OpenAPI input constraints without adding dependencies."""
    if "$ref" in schema:
        schema = schemas[schema["$ref"].split("/")[-1]]
    if "allOf" in schema:
        for option in schema["allOf"]:
            validate(value, option, schemas)
    if "anyOf" in schema or "oneOf" in schema:
        for option in schema.get("anyOf", schema.get("oneOf")):
            try:
                validate(value, option, schemas)
                return
            except Hold:
                pass
        raise Hold("Live schema union mismatch")
    kind = schema.get("type")
    types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    if kind in types and (
        not isinstance(value, types[kind])
        or kind in {"number", "integer"}
        and isinstance(value, bool)
    ):
        raise Hold("Live schema type mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise Hold("Live schema enum drift")
    if isinstance(value, dict):
        if set(schema.get("required", [])) - value.keys():
            raise Hold("Live schema requires additional input fields")
        properties = schema.get("properties", {})
        if properties and set(value) - properties.keys():
            raise Hold(
                "Planned input field is absent from live schema: "
                + ", ".join(sorted(set(value) - properties.keys()))
            )
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], schemas)
    if isinstance(value, list):
        for item in value:
            validate(item, schema.get("items", {}), schemas)
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            raise Hold("Live schema array bound drift")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (
            value < schema.get("minimum", -float("inf"))
            or value > schema.get("maximum", float("inf"))
        )
    ):
        raise Hold("Live schema numeric bound drift")
    if isinstance(value, str) and (
        len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9)
    ):
        raise Hold("Live schema string bound drift")


class Client:
    def __init__(self, provider, token, endpoint):
        if not token:
            raise Hold(f"Missing {provider} credential in environment")
        self.provider, self.endpoint = provider, endpoint
        self.base = (
            "https://api.replicate.com" if provider == "replicate" else "https://queue.fal.run"
        )
        self.hosts = (
            ["replicate.delivery"]
            if provider == "replicate"
            else ["fal.media", "fal.run", "storage.googleapis.com"]
        )
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            "Bearer " if provider == "replicate" else "Key "
        ) + token

    def http(self, method, url, **kwargs):
        allowed(url, [urlparse(self.base).hostname])
        response = self.session.request(
            method, url, timeout=(15, 75), allow_redirects=False, **kwargs
        )
        if response.status_code >= 300:
            # Never propagate a request/response representation that may echo credentials.
            raise HTTPFailure(self.provider, method, response.status_code)
        return response.json()

    def schema(self):
        if self.provider == "replicate":
            record = self.http("GET", f"{self.base}/v1/models/{self.endpoint}")
            schema = record["latest_version"]["openapi_schema"]
        else:
            response = requests.get(
                "https://fal.ai/api/openapi/queue/openapi.json",
                params={"endpoint_id": self.endpoint},
                timeout=30,
            )
            if response.status_code != 200:
                raise Hold(f"fal schema HTTP {response.status_code}")
            schema = response.json()
        schemas = schema["components"]["schemas"]
        if self.provider == "replicate":
            return schema, schemas["Input"], schemas
        for path in schema["paths"].values():
            post = path.get("post", {})
            entry = post.get("requestBody", {}).get("content", {}).get("application/json", {})
            if "schema" in entry:
                return schema, entry["schema"], schemas
        raise Hold("No fal input schema found")

    def submit(self, body):
        if self.provider == "replicate":
            return self.http(
                "POST",
                f"{self.base}/v1/models/{self.endpoint}/predictions",
                json={"input": body},
                headers={"Prefer": "wait=60"},
            )
        return self.http("POST", f"{self.base}/{self.endpoint}", json=body)

    def poll(self, record, observe, timeout=600):
        start = time.monotonic()
        if self.provider == "replicate":
            url = f"{self.base}/v1/predictions/{record['id']}"
        else:
            url = allowed(record["status_url"], ["queue.fal.run"])
        while True:
            observe(record)
            status = record.get("status")
            if status in {"succeeded", "failed", "canceled", "ERROR"}:
                return record
            if status == "COMPLETED":
                result = self.http("GET", record["response_url"])
                return {**record, "output": result}
            if time.monotonic() - start >= timeout:
                cancel = (
                    f"{url}/cancel" if self.provider == "replicate" else record.get("cancel_url")
                )
                if cancel:
                    self.http("POST" if self.provider == "replicate" else "PUT", cancel)
                raise Hold("Timed out; reservation retained, reconcile before proceeding")
            time.sleep(2)
            update = self.http("GET", url)
            record = {**record, **update}


class ReplicateClient(Client):
    def __init__(self, token, endpoint):
        super().__init__("replicate", token, endpoint)


class FalClient(Client):
    def __init__(self, token, endpoint):
        super().__init__("fal", token, endpoint)
