"""Public visitors' journeys over HTTP against real Gremlin and storage emulators.

Gremlin runs its GPU-free simulated worker and scene descriptions come from the local stand-ins
(`assist._local`): these tests prove analysis, automatic and tapped choices, orchestration,
delivery, recovery, isolation between visitors, budgets and quotas, not generation quality or
what a real model writes.
"""

import concurrent.futures
import io
import json

import av
import boto3
import requests
from boto3.dynamodb.conditions import Key
from gremlin_web.app import CLIENT_IP_HEADER, PROXY_HEADER, proxy_key
from gremlin_web.assist import FIXED_PROMPT
from gremlin_web.gremlin import Gremlin

from tests.conftest import STORAGE
from tests.harness import Browser

TERMINAL = ("succeeded", "failed", "cancelled")
SETTLED = ("succeeded", "failed", "cancelled", "awaiting_selection")


def ledger(site):
    """The caption budget's day counters: {sk: committed micro-USD}."""
    table = boto3.resource("dynamodb", endpoint_url=STORAGE[0], region_name="eu-north-1").Table(
        site.table
    )
    items = table.query(KeyConditionExpression=Key("pk").eq("BUDGET"))["Items"]
    return sum(int(i["n"]) for i in items if i["sk"].startswith("caption#day#"))


def jobs(site):
    """How many jobs Gremlin holds for the site."""
    gremlin = Gremlin(site.gremlin_url, site.gremlin_token)
    return len(gremlin.call("GET", "/api/imprint/jobs?limit=100").json()["jobs"])


def test_generate_watch_download_and_delete(site, media):
    browser = Browser(site)
    gid = browser.draft(media["reference"], media["driving"])
    # A double-clicked Generate starts one video.
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda _: browser.submit(gid), range(2)))
    assert all(a.ok for a in answers), [a.text for a in answers]
    assert {a.json()["id"] for a in answers} == {gid}
    assert browser.call("GET", "/api/v1/config").json()["active"] == gid

    # One video at a time across the site, like Gremlin's one active job.
    second = browser.draft(media["reference"], media["driving"])
    assert "Your other video" in browser.submit(second).json()["error"]["message"]
    stranger = Browser(site)
    theirs = stranger.draft(media["reference"], media["driving"])
    busy = stranger.submit(theirs).json()
    assert busy["error"]["code"] == "ONE_AT_A_TIME" and "Someone else" in busy["error"]["message"]
    assert stranger.call("GET", "/api/v1/config").json()["active"] is None

    # Nobody else can see, download, cancel or delete it, or use the visitor's draft.
    for method, path in (
        ("GET", f"/api/v1/generations/{gid}"),
        ("GET", f"/api/v1/generations/{gid}/files/result.mp4"),
        ("POST", f"/api/v1/generations/{gid}/cancel"),
    ):
        assert stranger.call(method, path, allow_redirects=False).status_code == 404
    stranger.call("DELETE", f"/api/v1/generations/{gid}")
    assert stranger.submit(second).status_code == 404
    assert stranger.call("GET", "/api/v1/results").status_code == 404

    view = browser.wait(gid)
    assert view["state"] == "succeeded", view
    assert view["duration_ms"] == 5000
    # The visitor never sees the description, the points, storage keys or Gremlin's identifiers.
    assert not {"tier", "seed", "prompt", "prompt_drafted"} & set(view)
    exposed = json.dumps(view)
    assert not any(word in exposed for word in ("prompt", "point", "key", "job_id", "sha256"))
    # The progress page's timeline is what the site and Gremlin recorded, in order.
    timeline = view["timeline"]
    analysis, gremlin = timeline["analysis"], timeline["gremlin"]
    assert analysis["clip_ms"] == 5000 and analysis["photo"]["chosen"] == "auto"
    assert view["submitted_at"] <= analysis["started_at"] <= analysis["finished_at"]
    assert analysis["finished_at"] <= timeline["description"]["at"] <= gremlin["sent_at"]
    assert gremlin["sent_at"] <= gremlin["stages"]["queued"] <= gremlin["gpu_requests"][0]
    # Gremlin's simulated worker reports a placed GPU before it takes the job.
    assert gremlin["gpu"] == "Simulated GPU"
    assert gremlin["gpu_requests"][0] <= gremlin["gpu_seen_at"] < gremlin["started_at"]
    assert {"preparation_seconds", "generation_seconds"} <= set(gremlin["timings"])
    delivery = timeline["delivery"]
    assert gremlin["started_at"] < delivery["started_at"] <= view["finished_at"]
    assert delivery["files"] == {name: f["bytes"] for name, f in view["files"].items()}
    assert timeline["retry"] is None and timeline["ended_stage"] is None
    assert set(view["files"]) == {"result.mp4", "poster.jpg"}
    assert browser.call("GET", "/api/v1/config").json()["active"] is None

    download = browser.call(
        "GET", f"/api/v1/generations/{gid}/files/result.mp4?download=true", allow_redirects=False
    )
    assert download.status_code == 302
    stored = requests.get(download.headers["Location"], timeout=30)
    assert stored.ok
    assert stored.headers["Cache-Control"] == "no-store"
    assert "attachment" in stored.headers["Content-Disposition"]
    assert len(stored.content) == view["files"]["result.mp4"]["bytes"]
    with av.open(io.BytesIO(stored.content)) as container:
        assert "AI-generated" in container.metadata.get("comment", "")
        assert sum(1 for _ in container.decode(video=0)) > 40

    # The draft that was refused can run now.
    assert browser.submit(second).ok
    assert browser.wait(second)["state"] == "succeeded"
    # Completion never grants another visitor access to delivered files.
    for name in ("result.mp4", "poster.jpg"):
        assert (
            stranger.call(
                "GET", f"/api/v1/generations/{gid}/files/{name}", allow_redirects=False
            ).status_code
            == 404
        )

    gremlin = Gremlin(site.gremlin_url, site.gremlin_token)
    before_jobs = gremlin.call("GET", "/api/imprint/jobs?limit=100").json()["jobs"]
    assert browser.call("DELETE", f"/api/v1/generations/{gid}").status_code == 204
    after_jobs = gremlin.call("GET", "/api/imprint/jobs?limit=100").json()["jobs"]
    assert sum(j["state"] == "cancelled" for j in after_jobs) == 1 + sum(
        j["state"] == "cancelled" for j in before_jobs
    )
    assert stranger.call("GET", "/api/v1/results").status_code == 404
    assert browser.call("GET", f"/api/v1/generations/{gid}").status_code == 404
    assert requests.get(download.headers["Location"], timeout=30).status_code == 404


def test_concurrent_submissions_admit_one_and_cancel_frees_the_slot(site, media):
    browser = Browser(site)
    drafts = [browser.draft(media["reference"], media["driving"]) for _ in range(3)]
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        answers = list(pool.map(browser.submit, drafts))
    accepted = [a for a in answers if a.ok]
    assert len(accepted) == 1, [a.text for a in answers]
    assert {a.json()["error"]["code"] for a in answers if not a.ok} == {"ONE_AT_A_TIME"}

    gid = accepted[0].json()["id"]
    cancel = browser.call("POST", f"/api/v1/generations/{gid}/cancel")
    if cancel.ok:
        assert browser.wait(gid)["state"] == "cancelled"
    else:
        # The simulated GPU leased it first: it runs to the end.
        assert cancel.json()["error"]["code"] == "ALREADY_STARTED"
        assert browser.wait(gid)["state"] == "succeeded"
    other = next(d for d in drafts if d != gid)
    assert browser.submit(other).ok
    assert browser.wait(other)["state"] == "succeeded"


def test_ambiguous_subjects_wait_for_one_tap_that_charges_nothing_twice(site, media):
    browser = Browser(site)
    before_jobs, before_budget = jobs(site), ledger(site)
    videos = browser.call("GET", "/api/v1/config").json()["left"]
    gid = browser.draft(media["two_people"], media["two_clip"])
    assert browser.submit(gid).ok
    view = browser.wait(gid, until=SETTLED)
    assert view["state"] == "awaiting_selection", view
    sides = view["selection"]["sides"]
    assert {side: s["why"] for side, s in sides.items()} == {
        "reference": "competing",
        "target": "competing",
    }
    assert len(sides["target"]["candidates"]) == 2 and view["cancellable"]
    revision = view["selection"]["revision"]
    # Waiting holds no slot or video, sends nothing to Gremlin and spends nothing on
    # descriptions.
    config = browser.call("GET", "/api/v1/config").json()
    assert (config["active"], config["left"]) == (gid, videos)
    assert (jobs(site), ledger(site)) == (before_jobs, before_budget)
    preview = browser.call(
        "GET", f"/api/v1/generations/{gid}/preview/target", allow_redirects=False
    )
    assert preview.status_code == 302
    assert requests.get(preview.headers["Location"], timeout=30).headers["Content-Type"] == (
        "image/jpeg"
    )

    # Another visitor can neither see the pictures nor choose.
    stranger = Browser(site)
    left, right = (c["point"] for c in sides["target"]["candidates"])
    photo_left = sides["reference"]["candidates"][0]["point"]
    assert (
        stranger.call("GET", f"/api/v1/generations/{gid}/preview/target", allow_redirects=False)
    ).status_code == 404
    assert (
        stranger.select(gid, revision, target_point=left, reference_point=photo_left).status_code
        == 404
    )
    # A stale page, a missing side and a second video are all refused; it stays pending.
    assert (
        browser.select(gid, "0" * 16, target_point=left, reference_point=photo_left).json()[
            "error"
        ]["code"]
        == "STALE"
    )
    assert browser.select(gid, revision, target_point=left).status_code == 422
    another = browser.draft(media["reference"], media["driving"])
    assert browser.submit(another).json()["error"]["code"] == "PENDING_SELECTION"

    chosen = browser.select(gid, revision, target_point=right, reference_point=photo_left)
    assert chosen.ok, chosen.text
    assert chosen.json()["state"] == "submitted"
    # The same tap again (a double click, a retry) is answered with the same video.
    again = browser.select(gid, revision, target_point=right, reference_point=photo_left)
    assert again.ok and again.json()["id"] == gid
    assert (
        browser.select(gid, revision, target_point=left, reference_point=photo_left).json()[
            "error"
        ]["code"]
        == "NOT_WAITING"
    )
    view = browser.wait(gid)
    assert view["state"] == "succeeded", view
    # One video was used, when the choice was confirmed; one description was paid for.
    assert browser.call("GET", "/api/v1/config").json()["left"] == videos - 1
    assert jobs(site) == before_jobs + 1
    assert ledger(site) == before_budget + 1685
    assert browser.call("GET", f"/api/v1/generations/{gid}/preview/target").status_code == 404
    # The other video may start now; it is not needed beyond that.
    assert browser.submit(another).ok
    assert browser.wait(another, until=SETTLED + ("processing",))["state"] != "failed"
    browser.call("POST", f"/api/v1/generations/{another}/cancel")


def test_a_prepared_video_can_be_cancelled_before_any_gpu_job(site, media):
    browser = Browser(site)
    before = jobs(site)
    gid = browser.draft(media["two_people"], media["two_clip"])
    assert browser.submit(gid).ok
    view = browser.wait(gid, until=SETTLED)
    assert view["state"] == "awaiting_selection"
    assert jobs(site) == before
    cancelled = browser.call("POST", f"/api/v1/generations/{gid}/cancel").json()
    assert (cancelled["state"], cancelled["failure"]["code"]) == ("cancelled", "cancelled")
    assert browser.call("GET", f"/api/v1/generations/{gid}/preview/target").status_code == 404
    assert browser.call("DELETE", f"/api/v1/generations/{gid}").status_code == 204


def test_the_accepted_fixtures_generate_with_complete_automatic_choices(site, media):
    browser = Browser(site)
    for name, kind, duration in (("enzo", "video/mp4", 5000), ("kungfu", "image/gif", 3400)):
        gid = browser.draft(media["screenshot"], media[name], driving_type=kind)
        assert browser.submit(gid).ok
        view = browser.wait(gid, until=SETTLED)
        # Clear subjects need no review: Generate goes straight to the result.
        assert view["state"] == "succeeded" and view["timeline"]["choice"]["asked_at"] is None
        assert view["duration_ms"] == duration
        analysis = view["timeline"]["analysis"]
        assert analysis["photo"]["chosen"] == analysis["clip"]["chosen"] == "auto"


def new_job(site, before):
    gremlin = Gremlin(site.gremlin_url, site.gremlin_token)
    jobs = gremlin.call("GET", "/api/imprint/jobs?limit=100").json()["jobs"]
    [job] = [g for g in jobs if g["id"] not in before]
    return job


def sent(site, gid):
    """What the site sent Gremlin for `gid`: (prompt, tier), from its own record."""
    table = boto3.resource("dynamodb", endpoint_url=STORAGE[0], region_name="eu-north-1").Table(
        site.table
    )
    [gen] = [i for i in table.scan()["Items"] if i.get("gid") == gid and "request" in i]
    return gen["request"]["prompt"], gen["request"]["tier"]


def job_ids(site):
    gremlin = Gremlin(site.gremlin_url, site.gremlin_token)
    return {g["id"] for g in gremlin.call("GET", "/api/imprint/jobs?limit=100").json()["jobs"]}


def test_caption_uncertainty_does_not_override_the_reviewed_subject(caption_sites, media):
    site = caption_sites["unclear"]
    browser = Browser(site)
    gid = browser.draft(media["reference"], media["driving"])
    assert browser.submit(gid).ok
    assert browser.wait(gid)["state"] == "succeeded"
    assert ledger(site) == 1685


def test_failed_descriptions_fall_back_to_the_fixed_one(caption_sites, media):
    for kind, charged in (("malformed", 2 * 1685), ("silent", 2 * 6200)):
        site = caption_sites[kind]
        before = job_ids(site)
        browser = Browser(site)
        gid = browser.draft(media["reference"], media["driving"])
        assert browser.submit(gid).ok
        assert browser.wait(gid)["state"] == "succeeded", kind
        # Two calls at most; an unusable answer is settled at its usage, a silent one keeps
        # its conservative reservation.
        assert ledger(site) == charged, kind
        new_job(site, before)
        assert sent(site, gid) == (FIXED_PROMPT, "complex-motion"), kind


def test_an_exhausted_description_budget_falls_back_without_a_call(caption_sites, media):
    site = caption_sites["closed"]
    before = job_ids(site)
    browser = Browser(site)
    assert browser.call("GET", "/api/v1/config").json()["preparing"] is True
    gid = browser.draft(media["reference"], media["driving"])
    assert browser.submit(gid).ok
    assert browser.wait(gid)["state"] == "succeeded"
    assert ledger(site) == 0
    new_job(site, before)
    assert sent(site, gid)[0] == FIXED_PROMPT


def test_a_flagged_public_figure_ends_before_any_gpu_work(caption_sites, media):
    site = caption_sites["public"]
    before = job_ids(site)
    browser = Browser(site)
    left = browser.call("GET", "/api/v1/config").json()["left"]
    gid = browser.draft(media["reference"], media["driving"])
    assert browser.submit(gid).ok
    view = browser.wait(gid, until=SETTLED)
    assert (view["state"], view["failure"]["code"]) == ("failed", "public_figure"), view
    assert job_ids(site) == before
    assert browser.call("GET", "/api/v1/config").json()["left"] == left


def test_the_fixed_description_generates_without_a_model_or_budget(caption_sites, media):
    site = caption_sites["fixed"]
    browser = Browser(site)
    assert browser.call("GET", "/api/v1/config").json()["preparing"] is True
    before = job_ids(site)
    gid = browser.draft(media["reference"], media["driving"])
    assert browser.submit(gid).ok
    assert browser.wait(gid)["state"] == "succeeded"
    assert ledger(site) == 0
    assert new_job(site, before)["tier"] == "complex-motion"


def test_gpu_side_rejection_explains_what_to_fix(rejecting_site, media):
    browser = Browser(rejecting_site)
    gid = browser.draft(media["reference"], media["driving"])
    assert browser.submit(gid).ok
    view = browser.wait(gid)
    assert view["state"] == "failed"
    assert view["failure"]["code"] == "reference_subject_not_found"
    assert "photo" in view["failure"]["message"].lower()
    # The progress page can show it stopped after a worker took the job.
    assert view["timeline"]["gremlin"]["started_at"] and view["timeline"]["ended_stage"]
    # The failed job does not hold Gremlin's or the site's slot.
    retry = browser.draft(media["reference"], media["driving"])
    assert browser.submit(retry).ok
    assert browser.wait(retry)["state"] == "failed"


def test_media_the_engine_cannot_use_fails_before_any_gpu_work(site, media):
    browser = Browser(site)
    before = jobs(site)
    for driving, code in (
        (b"not a video at all" * 100, "driving_unreadable"),
        (media["short_clip"], "driving_too_short"),
    ):
        gid = browser.draft(media["reference"], driving)
        assert browser.submit(gid).ok
        view = browser.wait(gid, until=SETTLED)
        assert (view["state"], view["selection"]["error"]["code"]) == ("awaiting_selection", code)
        assert browser.call("POST", f"/api/v1/generations/{gid}/cancel").ok
    assert jobs(site) == before


def test_other_websites_cannot_drive_a_local_site(site):
    body = {
        "reference": {"bytes": 10, "type": "image/png"},
        "driving": {"bytes": 10, "type": "video/mp4"},
    }
    for headers in ({}, {"Origin": "https://evil.example"}):
        refused = requests.post(site + "/api/v1/generations", json=body, headers=headers)
        assert refused.status_code == 403
    # A DNS-rebinding page reaches the server under its own host name.
    rebound = requests.get(site + "/api/v1/generations", headers={"Host": "evil.example:80"})
    assert rebound.status_code == 421
    # The custom domain's proxy reaches the Function URL under another host name; only the
    # proxy's key lets it in, since its forwarded address decides the per-network quotas.
    forged = {"Host": "evil.example:80", PROXY_HEADER: "guess", CLIENT_IP_HEADER: "203.0.113.9"}
    assert requests.get(site + "/api/v1/config", headers=forged).status_code == 403
    proxied = forged | {PROXY_HEADER: proxy_key(site.gremlin_token)}
    assert requests.get(site + "/api/v1/config", headers=proxied).ok


def test_everyone_shares_one_allowance_and_failures_give_it_back(limited_site, media):
    first, second = Browser(limited_site), Browser(limited_site)
    config = first.call("GET", "/api/v1/config").json()
    assert (config["left"], config["total"]) == (2, 2)
    # A video that never reaches the GPU uses nothing.
    bad = first.draft(media["reference"], media["short_clip"])
    assert first.submit(bad).ok
    view = first.wait(bad, until=SETTLED)
    if view["state"] == "awaiting_selection":
        first.call("POST", f"/api/v1/generations/{bad}/cancel")
    assert second.call("GET", "/api/v1/config").json()["left"] == 2

    # One visitor may use everything; a delivered video is not given back.
    for _ in range(2):
        gid = first.draft(media["reference"], media["driving"])
        assert first.submit(gid).ok
        assert first.wait(gid)["state"] == "succeeded"
    assert second.call("GET", "/api/v1/config").json()["left"] == 0
    # Nobody can start another, with a new cookie or not, nor even upload for one.
    refused = second.call(
        "POST",
        "/api/v1/generations",
        json={
            "reference": {"bytes": 10, "type": "image/png"},
            "driving": {"bytes": 10, "type": "video/mp4"},
        },
    ).json()
    assert refused["error"] == {"code": "LIMIT_REACHED", "message": "No videos are left."}


def test_uploads_are_limited_per_network(site, media):
    browser = Browser(site)
    body = {
        "reference": {"bytes": 10, "type": "image/png"},
        "driving": {"bytes": 10, "type": "video/mp4"},
    }
    too_big = dict(body, driving={"bytes": 101 * 1024**2, "type": "video/mp4"})
    assert browser.call("POST", "/api/v1/generations", json=too_big).status_code == 413
