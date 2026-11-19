"""Recovery with real local storage and controlled faults, plus benchmark HTTP submission.

No GPU or external model calls. Real Gremlin jobs here use its local simulated worker.
"""

import uuid

import pytest
from gremlin_web import generations as module
from gremlin_web import selection
from gremlin_web.db import Txn
from gremlin_web.local import provision
from gremlin_web.service import Service
from gremlin_web.settings import Settings

from tests.conftest import BUDGET, STORAGE

VISITOR, ADDRESS = "v" * 32, "192.0.2.1"


class Clock:
    def __init__(self):
        import time

        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def service(workdir):
    name = "gremlin-web-test-" + uuid.uuid4().hex[:12]
    settings = Settings(
        dynamodb_endpoint=STORAGE[0],
        s3_endpoint=STORAGE[1],
        table_name=name,
        media_bucket=name,
        caption_model="local",
        address_daily_videos=1000,
        **BUDGET,
    )
    clock = Clock()
    service = Service(settings, clock=clock)
    provision(service)
    service.clock_control = clock
    generations = service.generations
    # Driven step by step: no background invocation and Gremlin always open.
    generations.invoke = lambda visitor, gid: None
    generations.admission = lambda: {"accepting": True, "dispatching": True}
    return service


def submitted(service, reference, driving):
    generations = service.generations
    draft = generations.create_draft(
        VISITOR,
        ADDRESS,
        {
            "reference": {"bytes": len(reference), "type": "image/png"},
            "driving": {"bytes": len(driving), "type": "video/mp4"},
        },
    )
    gid = draft["id"]
    service.media.put(f"u/{gid}/reference", reference, "image/png")
    service.media.put(f"u/{gid}/driving", driving, "video/mp4")
    generations.submit(VISITOR, gid)
    return gid


def reviewing(generations, monkeypatch):
    """Hold clear proposals for confirmation, as when a person is reviewing the generation."""
    monkeypatch.setattr(
        generations, "_start_unreviewed", lambda gen, changes: generations._await(gen, [], changes)
    )


def reserved(service, media, monkeypatch):
    """A generation that was committed to the GPU, and so holds one video."""
    generations = service.generations
    monkeypatch.setattr(generations, "_create_job", lambda gen: None)
    gid = submitted(service, media["reference"], media["driving"])
    assert generations.process(VISITOR, gid) == "waiting"
    return gid


def keys(service, prefix):
    listing = service.media.s3.list_objects_v2(Bucket=service.settings.media_bucket, Prefix=prefix)
    return [item["Key"] for item in listing.get("Contents", ())]


def used(service):
    return service.settings.total_generations - service.quotas.left()


def spent(service):
    return sum(i["n"] for i in service.table.query("BUDGET") if i["sk"].startswith("caption#day#"))


def test_an_abandoned_choice_expires_and_its_inputs_are_deleted(service, media):
    generations = service.generations
    gid = submitted(service, media["reference"], media["two_clip"])
    assert generations.process(VISITOR, gid) == module.WAITING
    assert generations.active() is None
    assert keys(service, f"n/{gid}/")
    service.clock_control.now += module.SELECTION_SECONDS - 60
    assert service.sweep()["expired"] == 0
    service.clock_control.now += 120
    # Nobody comes back: the one-minute sweep ends it.
    assert service.sweep()["expired"] == 1
    gen = generations.get(VISITOR, gid)
    assert (gen["state"], gen["failure"]["code"]) == ("cancelled", "selection_expired")
    assert keys(service, f"n/{gid}/") == [] and keys(service, f"u/{gid}/") == []
    assert service.table.query("PENDING") == []


def test_correct_manual_points_survive_uncertainty_and_reach_submission(service, monkeypatch):
    from tests.benchmark import path

    generations = service.generations
    service.settings.caption_model = "fixed"
    for name in ("choose_reference", "analyze_clip"):
        original = getattr(selection, name)

        def uncertain(*args, original=original):
            return original(*args) | {"point": None, "subject": None, "why": "unsure"}

        monkeypatch.setattr(selection, name, uncertain)
    gid = submitted(service, path("reference").read_bytes(), path("driving").read_bytes())
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    points = {
        "reference_point": {"x": 0.474512345678, "y": 0.441023456789},
        "target_point": {"x": 0.463212345678, "y": 0.645823456789},
    }
    body = {"revision": gen["analysis"]["revision"]} | points
    generations.select(VISITOR, gid, body)
    reached = []
    monkeypatch.setattr(generations, "_create_job", lambda gen: reached.append(gen))
    assert generations.process(VISITOR, gid) == "waiting"
    assert len(reached) == 1
    assert {k: reached[0]["request"][k] for k in points} == points
    assert reached[0]["request"]["duration_ms"] == 3410
    assert generations.select(VISITOR, gid, body)["gid"] == gid
    assert used(service) == 1


def test_a_worker_killed_during_the_description_keeps_its_reservation_and_recovers(
    service, media, monkeypatch
):
    generations = service.generations
    gid = submitted(service, media["reference"], media["driving"])
    real = module.assist.caption
    # The first call dies after its reservation, like a timed-out Lambda.
    calls = []

    def dying(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("invocation killed")
        return real(*args, **kwargs)

    monkeypatch.setattr(module.assist, "caption", dying)
    monkeypatch.setattr(generations, "_create_job", lambda gen: None)
    assert generations.process(VISITOR, gid) == "error"
    gen = generations.get(VISITOR, gid)
    assert [a["status"] for a in gen["captions"]] == ["claimed"]
    service.clock_control.now = gen["next_at"]
    assert generations.process(VISITOR, gid) == "waiting"
    gen = generations.get(VISITOR, gid)
    assert [a["status"] for a in gen["captions"]] == ["unknown", "ok"]
    assert gen["request"]["prompt"] and gen["request"]["tier"] == "simple-motion"
    # The lost call is charged its full reservation, the answered one its usage.
    assert spent(service) == 6200 + 1685


def test_analysis_that_never_finishes_ends_instead_of_retrying_forever(service, media, monkeypatch):
    generations = service.generations
    gid = submitted(service, media["reference"], media["driving"])
    analyze = selection.analyze_clip

    def stuck(samples, duration_ms):
        raise MemoryError("out of memory")

    monkeypatch.setattr(selection, "analyze_clip", stuck)
    for _ in range(module.MAX_ANALYSES):
        assert generations.process(VISITOR, gid) == "error"
        service.clock_control.now = generations.get(VISITOR, gid)["next_at"]
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    assert gen["preparation_error"] == "media_too_complex"
    assert keys(service, f"u/{gid}/")
    assert generations.active() is None and spent(service) == 0
    monkeypatch.setattr(selection, "analyze_clip", analyze)
    generations.revise(VISITOR, gid, {"revision": gen["media_revision"]})
    assert generations.process(VISITOR, gid) == module.WAITING
    recovered = generations.get(VISITOR, gid)
    assert generations.view(recovered)["selection"]["can_generate"]
    assert recovered["media_revision"] != gen["media_revision"]
    assert used(service) == 0


def test_a_silent_model_falls_back_to_the_fixed_description_within_its_budget(
    service, media, monkeypatch
):
    generations = service.generations
    service.settings.caption_model = "local-silent"
    # Room for one reservation: the second call is refused by the budget, not attempted.
    service.budget.limits["day"] = 6200
    sent = []
    monkeypatch.setattr(generations, "_create_job", lambda g: sent.append(g))
    gid = submitted(service, media["reference"], media["driving"])
    assert generations.process(VISITOR, gid) == "describing"
    service.clock_control.now = generations.get(VISITOR, gid)["next_at"]
    assert generations.process(VISITOR, gid) == "waiting"
    [gen] = sent
    assert gen["request"]["prompt"] == module.assist.FIXED_PROMPT
    assert gen["description_source"] == "fixed"
    assert len(gen["captions"]) == 1 and spent(service) == 6200


def test_identical_inputs_reuse_the_stored_description(service, media, monkeypatch):
    generations = service.generations
    monkeypatch.setattr(generations, "_create_job", lambda g: None)
    calls = []
    caption = module.assist.caption
    monkeypatch.setattr(
        module.assist, "caption", lambda *a, **k: calls.append(1) or caption(*a, **k)
    )
    for _ in range(2):
        gid = submitted(service, media["reference"], media["driving"])
        assert generations.process(VISITOR, gid) == "waiting"
        assert generations.get(VISITOR, gid)["request"]["prompt"] != module.assist.FIXED_PROMPT
        generations._finish(generations.get(VISITOR, gid), "cancelled", "cancelled")
    assert len(calls) == 1 and spent(service) == 1685


def test_cancellation_during_a_description_call_does_not_reopen_preparation(
    service, media, monkeypatch
):
    generations = service.generations
    service.settings.caption_model = "local-malformed"
    gid = submitted(service, media["reference"], media["driving"])
    caption = module.assist.caption

    def cancelled_while_calling(*args, **kwargs):
        generations._update(generations.get(VISITOR, gid), {"cancel_requested": True})
        return caption(*args, **kwargs)

    monkeypatch.setattr(module.assist, "caption", cancelled_while_calling)
    assert generations.process(VISITOR, gid) == "describing"
    service.clock_control.now = generations.get(VISITOR, gid)["next_at"]
    assert generations.process(VISITOR, gid) == "cancelled"
    assert not service.table.query("PENDING")
    assert not keys(service, f"u/{gid}/")
    assert generations.active() is None


def test_each_network_starts_at_most_its_daily_videos(service, media, monkeypatch):
    generations = service.generations
    service.settings.caption_model = "fixed"
    service.quotas.daily_per_address = 1
    gid = reserved(service, media, monkeypatch)
    assert service.quotas.network_left(ADDRESS) == 0
    with pytest.raises(module.Rejected) as refused:
        submitted(service, media["reference"], media["driving"])
    assert refused.value.code == "LIMIT_REACHED" and "network" in refused.value.message
    # Ended before any GPU was used, the video goes back to the network too.
    generations._finish(generations.get(VISITOR, gid), "cancelled", "cancelled")
    assert service.quotas.network_left(ADDRESS) == 1 and used(service) == 0


def test_replenishing_gives_back_videos_but_not_uploads_or_the_description_budget(
    service, media, monkeypatch
):
    quotas = service.quotas
    service.settings.caption_model = "fixed"
    gid = reserved(service, media, monkeypatch)
    txn = Txn(service.table)
    service.budget.reserve(txn)
    txn.commit()
    assert used(service) == 1
    drafts, budget = quotas.used("drafts", ADDRESS, 3600), spent(service)
    assert service.operate({"web_operator": "replenish"}) == {"videos_left": 10}
    assert used(service) == 0
    assert (quotas.used("drafts", ADDRESS, 3600), spent(service)) == (drafts, budget) != (0, 0)
    # A generation reserved before the replenishment ending unbilled cannot go below zero.
    generations = service.generations
    generations._finish(generations.get(VISITOR, gid), "cancelled", "cancelled")
    assert used(service) == 0


def test_a_video_is_given_back_only_when_no_gpu_was_used(service, media, monkeypatch):
    generations = service.generations
    service.settings.caption_model = "fixed"
    for billed, expected in (
        (None, 0),
        ({"started": True}, 1),
        ({"gremlin": {"gpu_used": True}}, 2),
    ):
        gen = generations.get(VISITOR, reserved(service, media, monkeypatch))
        if billed:
            gen = generations._update(gen, billed)
        generations._finish(gen, "failed", "service")
        assert used(service) == expected, billed


def test_concurrent_reservations_never_exceed_the_allowance(service):
    import concurrent.futures

    from gremlin_web.db import optimistic
    from gremlin_web.visitors import LimitReached

    service.quotas.total = 3

    def reserve(_):
        try:
            optimistic(service.table, lambda txn: service.quotas.reserve(txn, None), attempts=60)
            return True
        except LimitReached:
            return False

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        admitted = list(pool.map(reserve, range(12)))
    assert admitted.count(True) == 3 and service.quotas.left() == 0


def test_results_and_abandoned_uploads_are_deleted_from_storage_on_time(service, media):
    generations = service.generations
    clock = service.clock_control
    service.settings.caption_model = "fixed"
    done = submitted(service, media["reference"], media["driving"])
    results = {}
    for name, kind in module.RESULTS.items():
        service.media.put(f"r/{done}/{name}", b"x" * 10, kind)
        results[name] = {"key": f"r/{done}/{name}", "bytes": 10, "sha256": ""}
    generations._finish(generations.get(VISITOR, done), "succeeded", None, results=results)
    # The finished generation's inputs are gone at once; another's in progress stay.
    assert keys(service, f"u/{done}/") == []
    running = submitted(service, media["reference"], media["driving"])
    abandoned = generations.create_draft(
        VISITOR,
        ADDRESS,
        {
            k: {"bytes": 10, "type": t}
            for k, t in (("reference", "image/png"), ("driving", "video/mp4"))
        },
    )["id"]
    service.media.put(f"u/{abandoned}/reference", b"x" * 10, "image/png")
    assert generations.file_url(VISITOR, done, "result.mp4", False)

    clock.now += module.DRAFT_SECONDS + 1
    report = service.sweep()
    assert (report["results_deleted"], report["drafts_deleted"]) == (0, 1)
    assert keys(service, f"u/{abandoned}/") == [] and generations.draft(VISITOR, abandoned) is None
    assert keys(service, f"r/{done}/")
    # Never an active job's files.
    assert generations.get(VISITOR, running)["state"] in module.ACTIVE
    assert len(keys(service, f"u/{running}/")) == 2

    clock.now += service.settings.retention_hours * 3600
    assert service.sweep()["results_deleted"] == 1
    assert keys(service, f"r/{done}/") == [] and generations.get(VISITOR, done) is None
    assert service.table.query("RESULT") == []


def test_interval_correction_fences_points_previews_hashes_and_submission(
    service, media, monkeypatch
):
    generations = service.generations
    service.settings.caption_model = "fixed"
    reviewing(generations, monkeypatch)
    gid = submitted(service, media["reference"], media["driving"])
    assert generations.process(VISITOR, gid) == module.WAITING
    original = generations.get(VISITOR, gid)
    old_revision = original["analysis"]["revision"]
    correction = {"revision": old_revision, "start_ms": 1035, "duration_ms": 3500}
    changed = generations.revise(VISITOR, gid, correction)
    assert (
        generations.revise(VISITOR, gid, correction)["media_revision"] == changed["media_revision"]
    )
    with pytest.raises(module.Rejected, match="changed"):
        generations.select(VISITOR, gid, {"revision": old_revision})
    assert generations.preview_url(VISITOR, gid, "target", old_revision) is None
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    assert gen["normalized"]["driving_sha256"] != original["normalized"]["driving_sha256"]
    assert gen["analysis"]["start_ms"] == 1035
    assert gen["normalized"]["first_source_ms"] < 1035
    assert gen["normalized"]["source_end_ms"] == 4535
    assert gen["selection"]["sources"] == {"reference": "auto", "target": "auto"}
    body = {
        "revision": gen["media_revision"],
        "prompt": "A snow leopard raises its forelegs in celebration on a football pitch.",
        "tier": "simple-motion",
    }
    generations.select(VISITOR, gid, body)
    submitted_requests = []
    monkeypatch.setattr(generations, "_create_job", lambda g: submitted_requests.append(g))
    assert generations.process(VISITOR, gid) == "waiting"
    [sent] = submitted_requests
    assert sent["request"]["start_ms"] == 0 and sent["request"]["duration_ms"] == 3500
    assert sent["request"]["prompt"] == body["prompt"] and sent["request"]["tier"] == body["tier"]
    assert sent["normalized"] == gen["normalized"]
    assert generations.select(VISITOR, gid, body)["gid"] == gid
    assert used(service) == 1


def test_uploads_are_refused_while_gremlin_is_closed_without_spending_the_allowance(service, media):
    generations = service.generations
    service.settings.address_hourly_drafts = 1
    files = {
        "reference": {"bytes": len(media["reference"]), "type": "image/png"},
        "driving": {"bytes": len(media["driving"]), "type": "video/mp4"},
    }
    generations.admission = lambda: {
        "accepting": True,
        "dispatching": False,
        "reason_code": "daily_limit_reached",
    }
    with pytest.raises(module.Rejected) as refused:
        generations.create_draft(VISITOR, ADDRESS, files)
    assert refused.value.code == "GREMLIN_UNAVAILABLE"
    assert "today's job limit" in refused.value.message
    # Reopened, the network's one upload of the hour is still there.
    generations.admission = lambda: {"accepting": True, "dispatching": True}
    assert generations.create_draft(VISITOR, ADDRESS, files)["id"]


def test_failed_preparation_can_replace_media_without_another_admission(service, media):
    generations = service.generations
    service.settings.caption_model = "fixed"
    gid = submitted(service, media["reference"], b"not a video")
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    assert gen["preparation_error"] == "driving_unreadable"
    assert keys(service, f"u/{gid}/") and generations.active() is None
    replacement = generations.create_draft(
        VISITOR,
        ADDRESS,
        {
            "reference": {"bytes": len(media["reference"]), "type": "image/png"},
            "driving": {"bytes": len(media["driving"]), "type": "video/mp4"},
        },
    )
    new_id = replacement["id"]
    service.media.put(f"u/{new_id}/reference", media["reference"], "image/png")
    service.media.put(f"u/{new_id}/driving", media["driving"], "video/mp4")
    body = {"revision": gen["media_revision"], "draft_id": new_id}
    generations.revise(VISITOR, gid, body)
    assert generations.revise(VISITOR, gid, body)["gid"] == gid
    assert generations.draft(VISITOR, new_id) is None
    assert generations.process(VISITOR, gid) == module.WAITING
    ready = generations.view(generations.get(VISITOR, gid))
    assert ready["selection"]["can_generate"] and ready["selection"]["error"] is None
    assert used(service) == 0
    generations.cancel(VISITOR, gid)
    assert not keys(service, f"u/{new_id}/") and not keys(service, f"u/{gid}/")


def test_selection_rechecks_revision_inside_its_transaction(service, media, monkeypatch):
    generations = service.generations
    service.settings.caption_model = "fixed"
    reviewing(generations, monkeypatch)
    gid = submitted(service, media["reference"], media["driving"])
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    revision = gen["media_revision"]
    admit = generations._admit

    def race(decide):
        monkeypatch.setattr(generations, "_admit", admit)
        generations.revise(
            VISITOR, gid, {"revision": revision, "start_ms": 1000, "duration_ms": 3000}
        )
        return admit(decide)

    monkeypatch.setattr(generations, "_admit", race)
    with pytest.raises(module.Rejected, match="changed"):
        generations.select(VISITOR, gid, {"revision": revision})
    assert not generations.get(VISITOR, gid).get("selection_receipt")


def test_explicit_candidate_identity_is_never_inferred_from_nested_boxes(service, media):
    generations = service.generations
    service.settings.caption_model = "fixed"
    gid = submitted(service, media["reference"], media["two_clip"])
    assert generations.process(VISITOR, gid) == module.WAITING
    gen = generations.get(VISITOR, gid)
    point = {"x": 0.512345678901, "y": 0.612345678901}
    gen["analysis"]["target"]["candidates"] = [
        {"label": "person", "box": [0.1, 0.1, 0.9, 0.9], "point": point},
        {"label": "person", "box": [0.4, 0.4, 0.7, 0.8], "point": point},
    ]
    gen = generations._update(gen, {})
    chosen = generations.select(
        VISITOR,
        gid,
        {"revision": gen["media_revision"], "target_point": point, "target_candidate": 0},
    )
    assert chosen["selection"]["target"] == 0
    assert chosen["selection"]["target_point"] == point


@pytest.mark.parametrize("uncertain", [False, True])
def test_exact_benchmark_through_http_to_real_gremlin_submission(
    service, site, monkeypatch, uncertain
):
    import time

    from fastapi.testclient import TestClient
    from gremlin_web.app import create_app
    from gremlin_web.gremlin import Gremlin
    from gremlin_web.visitors import COOKIE, visitor_of

    from tests.benchmark import path

    generations = service.generations
    service.settings.caption_model = "fixed"
    generations.gremlin = Gremlin(site.gremlin_url, site.gremlin_token)
    if uncertain:
        for name in ("choose_reference", "analyze_clip"):
            original = getattr(selection, name)

            def ask(*args, original=original):
                return original(*args) | {"point": None, "subject": None, "why": "unsure"}

            monkeypatch.setattr(selection, name, ask)
    sent = []
    create = generations.gremlin.create

    def capture(intent):
        sent.append(intent)
        return create(intent)

    monkeypatch.setattr(generations.gremlin, "create", capture)
    client = TestClient(create_app(service), base_url=service.settings.public_origin)
    client.headers["Origin"] = service.settings.public_origin
    photo, clip = path("reference").read_bytes(), path("driving").read_bytes()
    draft = client.post(
        "/api/v1/generations",
        json={
            "reference": {"bytes": len(photo), "type": "image/jpeg"},
            "driving": {"bytes": len(clip), "type": "video/mp4"},
        },
    ).json()
    gid = draft["id"]
    visitor = visitor_of(client.cookies[COOKIE])
    service.media.put(f"u/{gid}/reference", photo, "image/jpeg")
    service.media.put(f"u/{gid}/driving", clip, "video/mp4")
    url = f"/api/v1/generations/{gid}"
    assert client.post(url + "/submit").is_success
    points = {}
    if uncertain:
        assert generations.process(visitor, gid) == module.WAITING
        prepared = client.get(url).json()["selection"]
        assert prepared["required"] == ["reference", "target"]
        assert prepared["interval"]["end_ms"] == 3410
        assert (
            client.post(
                url + "/select",
                json={"revision": prepared["revision"], "target_point": {"x": True, "y": 0.5}},
            ).status_code
            == 422
        )
        points = {
            "reference_point": {"x": 0.474512345678, "y": 0.441023456789},
            "target_point": {"x": 0.463212345678, "y": 0.645823456789},
        }
        body = {"revision": prepared["revision"]} | points
        for _ in range(2):
            assert client.post(url + "/select", json=body).is_success
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        generations.process(visitor, gid)
        gen = generations.get(visitor, gid)
        if gen["state"] in module.TERMINAL:
            break
        time.sleep(0.3)
    assert gen["state"] == "succeeded", gen
    assert len(sent) == 1
    intent = sent[0]
    assert intent["start_ms"] == 0 and intent["duration_ms"] == 3410
    assert intent["tier"] == "complex-motion" and "character from the reference" in intent["prompt"]
    assert {k: intent[k] for k in points} == points
    assert used(service) == 1 and spent(service) == 0
