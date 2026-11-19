"""Real Chromium interaction: native pointer events and controlled upload races.

Uses installed Chrome, local storage and the simulated worker. No GPU or external model.
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture
def page(fixed_site, request):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: print(f"Browser error: {error}"))
        page.goto(fixed_site)
        expect(page.locator('[data-file="photo"]')).to_be_enabled(timeout=30000)
        try:
            yield page
        finally:
            evidence = (
                Path(__file__).resolve().parents[2]
                / "artifacts/generation-recovery-2026-11-03/browser"
            )
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(evidence / (request.node.name + ".png")), full_page=True)
            browser.close()


def files(tmp_path, media):
    photo, clip = tmp_path / "photo.png", tmp_path / "clip.mp4"
    photo.write_bytes(media["two_people"])
    clip.write_bytes(media["two_clip"])
    return photo, clip


def play(page, selector):
    video = page.locator(selector)
    expect(video).to_be_visible()
    video.evaluate("video => video.play()")
    expect(video).to_have_js_property("paused", False)
    expect(video).to_have_js_property("readyState", 4, timeout=30000)
    page.wait_for_timeout(250)
    assert video.evaluate("video => video.currentTime") > 0
    video.evaluate("video => video.pause()")


@pytest.mark.parametrize("width", [1280, 390])
def test_real_pointer_coordinates_survive_candidate_overlays(page, tmp_path, media, width):
    page.set_viewport_size({"width": width, "height": 900})
    photo, clip = files(tmp_path, media)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    images = page.locator(".picker img")
    expected = {}
    for side, image in zip(("reference", "target"), images.all(), strict=True):
        expect(image).to_be_visible()
        image.evaluate("img => img.decode()")
        image.evaluate(
            """img => img.addEventListener('click', e => {
                const r=img.getBoundingClientRect();
                window.lastPoint={x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height};
            })"""
        )
        image.scroll_into_view_if_needed()
        rect = image.bounding_box()
        page.mouse.click(rect["x"] + rect["width"] * 0.30, rect["y"] + rect["height"] * 0.63)
        expected[f"{side}_point"] = page.evaluate("window.lastPoint")
    # The same picker works from the keyboard: Shift+Right moves the point a tenth across.
    target = page.locator(".picker").nth(1)
    target.focus()
    target.press("Shift+ArrowRight")
    point = expected["target_point"]
    expected["target_point"] = {
        "x": round(min(1, point["x"] + 0.1) * 1000) / 1000,
        "y": round(point["y"] * 1000) / 1000,
    }
    expect(page.locator(".choose-side [aria-live]").nth(1)).to_contain_text("across")
    with page.expect_request("**/select") as sent:
        page.locator("[data-continue]").click()
    payload = sent.value.post_data_json
    assert {key: payload[key] for key in expected} == expected
    expect(page.locator("[data-result]")).to_be_visible(timeout=60000)
    play(page, "[data-player]")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("side", ["photo", "clip"])
def test_replacement_is_fenced_while_prepare_awaits_upload(page, tmp_path, media, side):
    photo, clip = files(tmp_path, media)
    pending = []

    def hold(route):
        if route.request.method == "PUT" and not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/*", hold)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    expect(page.locator("[data-upload-progress]")).to_be_visible()
    page.locator("[data-submit]").click()
    expect(page.locator(f'[data-file="{side}"]')).to_be_disabled()
    pending[0].continue_()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    page.locator("[data-cancel-choice]").click()
    expect(page.locator("[data-title]")).to_have_text("Cancelled")


@pytest.mark.parametrize("close_method", ["button", "escape", "navigate"])
def test_private_preview_deletes_on_close(page, close_method):
    from tests.benchmark import path

    page.set_viewport_size({"width": 390, "height": 844})
    before = page.request.get(page.url + "api/v1/config").json()
    page.locator('[data-file="photo"]').set_input_files(path("reference"))
    page.locator('[data-file="clip"]').set_input_files(path("driving"))
    page.locator("[data-submit]").click()
    expect(page.locator("[data-result]")).to_be_visible(timeout=90000)
    expect(page).to_have_title("Adding to the Slop")
    assert page.locator("[data-choose]").is_hidden()
    play(page, "[data-player]")
    gid = page.url.rsplit("/", 1)[1]
    origin = page.url.split("/videos/")[0]
    file_path = f"{origin}/api/v1/generations/{gid}/files/result.mp4"
    signed = page.request.get(file_path, max_redirects=0).headers["location"]
    if close_method == "button":
        # Failed deletion stays visible and retryable.
        endpoint = f"**/api/v1/generations/{gid}"
        page.route(endpoint, lambda route: route.fulfill(status=503, body="{}"))
        page.locator("[data-close-result]").click()
        expect(page.locator("[data-preview-error]")).to_contain_text("Could not delete")
        expect(page.locator("[data-result]")).to_be_visible()
        page.unroute(endpoint)
        page.locator("[data-close-result]").click()
    elif close_method == "escape":
        page.keyboard.press("Escape")
    else:
        page.goto(origin + "/")
    expect(page).to_have_url(origin + "/")
    # Keepalive deletion may complete after navigation.
    import time

    deadline = time.monotonic() + 10
    while page.request.get(file_path, max_redirects=0).status != 404:
        assert time.monotonic() < deadline, "preview media was not deleted"
        page.wait_for_timeout(100)
    assert page.request.get(signed).status == 404
    assert page.request.get(f"{origin}/api/v1/generations/{gid}").status == 404
    after = page.request.get(origin + "/api/v1/config").json()
    assert after["left"] == before["left"] - 1


@pytest.mark.parametrize("width", [1280, 390, 320])
def test_quota_bars_follow_config_and_terms_stay_accessible(page, width):
    page.set_viewport_size({"width": width, "height": 844})
    origin = page.url.rstrip("/")
    config = page.request.get(origin + "/api/v1/config").json()
    page.route("**/api/v1/config", lambda route: route.fulfill(json=config))
    quota = page.locator("[data-quota]")
    bars = quota.locator(".quota-bars span")
    # Catch stale counts, singular/plural, exhaustion, disabled allowances and a
    # changed backend total without needing paid or repeated video generations.
    for total, left, lit in [
        (10, 10, 10),
        (10, 7, 7),
        (10, 1, 1),
        (10, 0, 0),
        (0, 0, 0),
        (2, 1, 5),
    ]:
        config.update(total=total, left=left)
        if total == 2:
            config["gremlin"] = {"ready": False, "reason_code": "daily_limit_reached"}
        page.reload()
        label = f"{left} generation{'s' if left != 1 else ''} left"
        expect(quota).to_have_attribute("aria-label", label)
        expect(quota).to_have_attribute("title", label)
        expect(bars).to_have_count(10)
        expect(page.locator("[data-submit]")).to_have_text("None left" if left == 0 else "Generate")
        # Nothing can start: the page says why and doesn't take uploads it would throw away.
        blocked = left == 0 or not config["gremlin"]["ready"]
        expect(page.locator("[data-blocked]")).to_be_visible() if blocked else expect(
            page.locator("[data-blocked]")
        ).to_be_hidden()
        assert page.locator('[data-file="photo"]').is_disabled() == blocked
        assert bars.evaluate_all("bars => bars.map(b => +getComputedStyle(b).opacity)") == [
            1 if i < lit else 0.2 for i in range(10)
        ]
        rects = bars.evaluate_all("bars => bars.map(b => b.getBoundingClientRect().toJSON())")
        assert len({r["y"] for r in rects}) == 1
        assert all(r["width"] > r["height"] for r in rects)
        assert all(0 <= r["x"] < r["right"] <= width for r in rects)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("[data-submit]")).to_have_text("Generate")
    notice = page.locator("[data-submit] + .fine")
    expect(notice).to_have_text("Private preview · Deleted when you close it")
    assert notice.locator("a").count() == 0
    # Terms sit in the header's info menu, which stays closed until asked for.
    about = page.get_by_role("button", name="About", exact=True)
    expect(about).to_have_attribute("aria-expanded", "false")
    about.click()
    expect(about).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("link", name="Terms", exact=True).click()
    expect(page.get_by_role("heading", name="Terms", exact=True)).to_be_visible()
    expect(page).to_have_title("Adding to the Slop")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # The icon declarations must resolve to decodable assets with their real MIME types.
    for selector, content_type in [
        ('link[rel="icon"][type="image/svg+xml"]', "image/svg+xml"),
        ('link[rel="icon"][type="image/vnd.microsoft.icon"]', "image/vnd.microsoft.icon"),
        ('link[rel="apple-touch-icon"]', "image/png"),
    ]:
        url = page.locator(selector).get_attribute("href")
        response = page.request.get(origin + url)
        assert response.ok and response.headers["content-type"] == content_type
        page.evaluate(
            """url => new Promise((resolve, reject) => {
                const img = new Image();
                img.onload = () => resolve(true);
                img.onerror = reject;
                img.src = url;
            })""",
            url,
        )
    fallback = page.request.get(origin + "/favicon.ico")
    assert fallback.ok and fallback.headers["content-type"] == "image/vnd.microsoft.icon"


def test_review_reload_interval_and_keyboard_choice(page, tmp_path, media):
    page.set_viewport_size({"width": 390, "height": 844})
    photo, clip = files(tmp_path, media)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose-title]")).to_have_text("Choose the characters", timeout=60000)
    play(page, "[data-selected-clip]")
    page.locator("[data-source-preview] summary").click()
    play(page, "[data-source-clip]")
    page.reload()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=30000)
    api_url = page.url.replace("/videos/", "/api/v1/generations/")
    previous = page.request.get(api_url).json()
    page.locator("[data-interval-start]").fill("0.1")
    page.locator("[data-interval-duration]").fill("3.1")
    expect(page.locator("[data-continue]")).to_be_disabled()
    page.locator("[data-prepare]").click()
    expect(page.locator("[data-interval-note]")).to_contain_text("0.100 to 3.200", timeout=60000)
    expect(page.locator("[data-choose-title]")).to_have_text("Choose the characters")
    stale = []

    def stale_poll(route):
        if route.request.method == "GET" and not stale:
            stale.append(True)
            route.fulfill(json=previous)
        else:
            route.continue_()

    page.route(api_url, stale_poll)
    with page.expect_response(api_url, timeout=20000):
        page.wait_for_timeout(16000)
    expect(page.locator("[data-interval-note]")).to_contain_text("0.100 to 3.200")
    assert stale
    page.unroute(api_url, stale_poll)
    for candidate in page.locator(".candidate-controls").all():
        button = candidate.get_by_role("button").first
        button.focus()
        button.press("Enter")
        expect(button).to_have_attribute("aria-pressed", "true")
    with page.expect_request("**/select") as sent:
        page.locator("[data-continue]").click()
    body = sent.value.post_data_json
    assert body["reference_candidate"] == 0 and body["target_candidate"] == 0
    expect(page.locator("[data-result]")).to_be_visible(timeout=60000)
    play(page, "[data-player]")


def test_preparation_recovers_after_a_lost_response_without_duplicate_submission(
    page, tmp_path, media
):
    photo, clip = files(tmp_path, media)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    attempts = []

    def lose_first_response(route):
        attempts.append(route.request.url)
        response = route.fetch()
        if len(attempts) == 1:
            route.abort("failed")
        else:
            route.fulfill(response=response)

    page.route("**/submit", lose_first_response)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-error]")).to_contain_text("recover this same preparation")
    expect(page.locator('[data-file="photo"]')).to_be_disabled()
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    assert len(attempts) == 2 and len(set(attempts)) == 1
    page.locator("[data-cancel-choice]").click()
    expect(page.locator("[data-title]")).to_have_text("Cancelled")


def test_latest_upload_wins_and_pending_media_can_be_replaced(page, tmp_path, media):
    photo, clip = files(tmp_path, media)
    replacement = tmp_path / "one.png"
    replacement.write_bytes(media["reference"])
    drafts, pending = [], []

    def uploads(route):
        request = route.request
        if request.method == "POST" and request.url.endswith("/api/v1/generations"):
            response = route.fetch()
            drafts.append(response.json()["id"])
            route.fulfill(response=response)
        elif request.method == "PUT" and not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/*", uploads)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    expect(page.locator("[data-upload-progress]")).to_be_visible()
    page.locator('[data-file="photo"]').set_input_files(replacement)
    expect(page.locator("[data-upload-progress]")).to_be_hidden(timeout=30000)
    pending[0].continue_()
    with page.expect_request("**/submit") as submitted:
        page.locator("[data-submit]").click()
    assert len(drafts) == 2 and f"/{drafts[-1]}/submit" in submitted.value.url
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    gid = page.url.rsplit("/", 1)[1]
    api_url = page.url.replace("/videos/", "/api/v1/generations/")
    before = page.request.get(api_url)
    assert before.ok
    page.locator("[data-replace-files]").click()
    expect(page.locator('[data-file="photo"]')).to_be_enabled()
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    assert page.url.endswith(f"/videos/{gid}")
    after = page.request.get(api_url).json()
    assert after["selection"]["revision"] != before.json()["selection"]["revision"]
    assert len(after["selection"]["sides"]["reference"]["candidates"]) == 2
    page.locator("[data-cancel-choice]").click()
    expect(page.locator("[data-title]")).to_have_text("Cancelled")


def test_interrupted_polling_reports_stale_progress_and_recovers(page, tmp_path, media):
    photo, clip = files(tmp_path, media)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    api_url = page.url.replace("/videos/", "/api/v1/generations/")
    page.route(api_url, lambda route: route.abort("internetdisconnected"))
    expect(page.locator("[data-error]")).to_contain_text("Progress is stale", timeout=30000)
    page.wait_for_timeout(1100)
    elapsed = page.locator("[data-elapsed]").inner_text()
    page.wait_for_timeout(1300)
    assert page.locator("[data-elapsed]").inner_text() == elapsed
    page.unroute(api_url)
    expect(page.locator("[data-error]")).to_be_hidden(timeout=20000)
    page.locator("[data-cancel-choice]").click()
    expect(page.locator("[data-title]")).to_have_text("Cancelled")


def test_a_new_preparation_error_is_shown_even_when_media_did_not_change(page, tmp_path, media):
    photo, clip = files(tmp_path, media)
    page.locator('[data-file="photo"]').set_input_files(photo)
    page.locator('[data-file="clip"]').set_input_files(clip)
    page.locator("[data-submit]").click()
    expect(page.locator("[data-choose]")).to_be_visible(timeout=60000)
    api_url = page.url.replace("/videos/", "/api/v1/generations/")
    # A confirmation reply is lost; by the next poll the server has returned to review
    # with a preparation error. Its media revision is unchanged.
    failed = page.request.get(api_url).json()
    failed["version"] += 1
    failed["selection"]["until"] += 10
    failed["selection"]["error"] = {
        "code": "driving_scene_cut",
        "message": "No usable continuous shot was found in the selected part.",
    }
    failed["selection"]["can_generate"] = False
    page.route(api_url, lambda route: route.fulfill(json=failed))
    expect(page.locator("[data-choose-error]")).to_contain_text(
        "No usable continuous shot", timeout=20000
    )
    expect(page.locator("[data-choose-title]")).to_have_text("Preparation needs attention")
    page.unroute(api_url)
    page.reload()
    expect(page.locator("[data-choose]")).to_be_visible()
    page.locator("[data-cancel-choice]").click()
    expect(page.locator("[data-title]")).to_have_text("Cancelled")
