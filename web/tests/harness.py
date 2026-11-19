"""A visitor's browser for the journey tests, over the processes `devtools.local_stack` runs."""

from __future__ import annotations

import time

import requests


class Browser:
    """A browser: a cookie jar, the site's Origin on writes, direct uploads to storage."""

    def __init__(self, origin):
        self.origin = origin
        self.http = requests.Session()
        self.http.headers["Origin"] = origin

    def call(self, method, path, **kwargs):
        return self.http.request(method, self.origin + path, timeout=60, **kwargs)

    def draft(
        self, reference: bytes, driving: bytes, driving_type="video/mp4", reference_type="image/png"
    ):
        """Create a draft and upload both files; returns its ID."""
        response = self.call(
            "POST",
            "/api/v1/generations",
            json={
                "reference": {"bytes": len(reference), "type": reference_type},
                "driving": {"bytes": len(driving), "type": driving_type},
            },
        )
        assert response.ok, response.text
        draft = response.json()
        for kind, data in (("reference", reference), ("driving", driving)):
            link = draft["uploads"][kind]
            put = requests.put(link["url"], data=data, headers=link["headers"], timeout=60)
            assert put.ok, put.text
        return draft["id"]

    def submit(self, gid):
        return self.call("POST", f"/api/v1/generations/{gid}/submit")

    def select(self, gid, revision, **points):
        return self.call(
            "POST", f"/api/v1/generations/{gid}/select", json={"revision": revision} | points
        )

    def wait(self, gid, until=("succeeded", "failed", "cancelled"), seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.call("GET", f"/api/v1/generations/{gid}").json()
            if (
                view["state"] == "awaiting_selection"
                and "awaiting_selection" not in until
                and not view["selection"]["required"]
                and view["selection"]["can_generate"]
            ):
                confirmed = self.select(gid, view["selection"]["revision"])
                assert confirmed.ok, confirmed.text
                continue
            if view["state"] in until:
                return view
            time.sleep(0.5)
        raise AssertionError(f"generation {gid} stuck in {view}")
