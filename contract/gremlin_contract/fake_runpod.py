"""An in-process HTTP double of the RunPod endpoint API; it never contacts the provider.

Management requests arrive under `/mgmt/v2/serverless/<endpoint>` and job requests under
`/jobs/v2/<endpoint>/...`. Tests change its fields directly to script provider behaviour.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class FakeRunPod:
    def __init__(self):
        self.calls = []
        self.submissions = []
        self.webhooks = []
        self.patches = []
        self.workers = {"min": 0, "max": 0, "idleTimeout": 60}
        self.endpoint = {}
        self.model_references = []
        self.graphql_payload = None
        self.worker_records = []
        self.catalog = {"gpus": []}
        self.data_centers = {"dataCenters": []}
        self.network_volumes = {}
        self.status_code = 200
        self.status = {"status": "IN_QUEUE"}
        self.cancel_code = 200
        # "GET" or "PATCH": that management request drops its connection.
        self.management_failure = None
        # "http" answers 500; "transport" drops the connection.
        self.status_failure = None
        self.lose_submission_response = False
        # Like RunPod's job API, which sees a capacity change seconds late: this many further
        # submissions are refused as paused, as is every one while the maximum is zero.
        self.paused_submissions = 0
        self.authorizations = set()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def cancels(self):
        return [path for method, path in self.calls if method == "POST" and "/cancel/" in path]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def _reply(self, code, body=None):
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _drop(self):
                # No status line at all: the client sees a lost response.
                self.close_connection = True

            def _handle(self):
                body = self._body()
                fake.calls.append((self.command, self.path))
                fake.authorizations.add(self.headers.get("Authorization"))
                path = urlsplit(self.path).path
                if path == "/graphql":
                    return self._reply(
                        200,
                        fake.graphql_payload
                        if fake.graphql_payload is not None
                        else {
                            "data": {
                                "myself": {"endpoint": {"modelReferences": fake.model_references}}
                            }
                        },
                    )
                if path == "/mgmt/v2/catalog/gpus":
                    return self._reply(200, fake.catalog)
                if path == "/mgmt/v2/catalog/datacenters":
                    return self._reply(200, fake.data_centers)
                if path.startswith("/mgmt/v2/network-volumes/"):
                    volume = fake.network_volumes.get(path.rsplit("/", 1)[-1])
                    return self._reply(200 if volume else 404, volume)
                if path.startswith("/mgmt/v2/serverless/") and path.endswith("/workers"):
                    return self._reply(200, {"workers": list(fake.worker_records)})
                if self.path.startswith("/mgmt/v2/serverless/"):
                    if fake.management_failure == self.command:
                        return self._drop()
                    if self.command == "PATCH":
                        fake.patches.append(body)
                        fake.workers |= body["workers"]
                        fake.endpoint |= {k: v for k, v in body.items() if k != "workers"}
                        return self._reply(200, {})
                    # The real v2 REST surface omits the separate GraphQL model reference.
                    endpoint = {
                        key: value
                        for key, value in fake.endpoint.items()
                        if key != "modelReferences"
                    }
                    return self._reply(200, endpoint | {"workers": dict(fake.workers)})
                if self.path.endswith("/run"):
                    if fake.workers["max"] < 1 or fake.paused_submissions > 0:
                        fake.paused_submissions = max(0, fake.paused_submissions - 1)
                        return self._reply(409, {"status": 409, "code": "ENDPOINT_PAUSED"})
                    fake.submissions.append(body["input"])
                    if "webhook" in body:
                        fake.webhooks.append(body["webhook"])
                    if fake.lose_submission_response:
                        return self._drop()
                    return self._reply(200, {"id": f"provider-job-{len(fake.submissions)}"})
                if "/cancel/" in self.path:
                    return self._reply(fake.cancel_code, {})
                if "/status/" in self.path:
                    if fake.status_failure == "transport":
                        return self._drop()
                    if fake.status_failure == "http":
                        return self._reply(500, {"error": "unavailable"})
                    return self._reply(fake.status_code, dict(fake.status))
                return self._reply(404, {"error": "unknown"})

            do_GET = do_POST = do_PATCH = _handle

        return Handler
