"""The Gremlin generation API, called as one direct client (docs/api.md, contract version 3).

The site's own client identity is the scope of its jobs, idempotency keys and Gremlin's one
active job, so no owner header is sent. Every call here is safe to repeat.
"""

from __future__ import annotations

import time

import requests

RETRYABLE = {502, 504}


class GremlinError(Exception):
    def __init__(self, status, code, message, reason=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.reason = status, code, message, reason


class Gremlin:
    def __init__(self, base, token, *, attempts=3):
        self.base = base.rstrip("/")
        self.token = token
        self.attempts = attempts
        self.http = requests.Session()

    def call(self, method, path, *, timeout=(5, 25), **kwargs):
        headers = {"Authorization": "Bearer " + self.token}
        for attempt in range(self.attempts):
            try:
                response = self.http.request(
                    method, self.base + path, headers=headers, timeout=timeout, **kwargs
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.attempts - 1:
                    raise
            else:
                if response.ok:
                    return response
                detail = None
                try:
                    body = response.json()
                    detail = body.get("detail") if isinstance(body, dict) else body
                except ValueError:
                    pass
                coded = isinstance(detail, dict) and "code" in detail
                transient = response.status_code in RETRYABLE or (
                    response.status_code in {429, 500, 503} and not coded
                )
                if not transient or attempt == self.attempts - 1:
                    if coded:
                        raise GremlinError(
                            response.status_code,
                            detail["code"],
                            detail.get("message", ""),
                            detail.get("reason"),
                        )
                    raise GremlinError(response.status_code, None, str(detail)[:300])
            time.sleep(1.5**attempt)

    def capabilities(self):
        return self.call("GET", "/api/imprint/capabilities").json()

    def create(self, intent):
        return self.call("POST", "/api/imprint/jobs", json=intent).json()

    def job(self, job_id):
        return self.call("GET", f"/api/imprint/jobs/{job_id}").json()

    def upload(self, job_id, kind, data):
        link = self.call(
            "POST", f"/api/imprint/jobs/{job_id}/inputs/{kind}/upload", json={"bytes": len(data)}
        ).json()
        # No bearer token goes to storage: the signed link is the authorization.
        response = requests.request(
            link["method"], link["url"], data=data, headers=link["headers"], timeout=(10, 120)
        )
        if not response.ok:
            raise GremlinError(response.status_code, None, "storage refused the upload")

    def submit(self, job_id):
        # Submit runs Gremlin's preflight and safety checks: allow its full 30 s budget.
        return self.call("POST", f"/api/imprint/jobs/{job_id}/submit", timeout=(5, 40)).json()

    def cancel(self, job_id, *, if_not_started):
        params = {"if_not_started": "true"} if if_not_started else None
        return self.call("DELETE", f"/api/imprint/jobs/{job_id}", params=params).json()

    def result(self, job_id, name):
        return self.call(
            "GET", f"/api/imprint/jobs/{job_id}/results/{name}", timeout=(5, 60)
        ).content

    def saved(self, job_id):
        self.call("POST", f"/api/imprint/jobs/{job_id}/saved")
