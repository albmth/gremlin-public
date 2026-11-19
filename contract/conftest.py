import os

import pytest
from gremlin_contract.fake_runpod import FakeRunPod
from gremlin_contract.harness import Api, Backend

# A small explicit envelope; spending authority is never a default.
DEFAULT_SETTINGS = {
    "imprint_hourly_microusd": "650000",
    "imprint_budget_microusd": "5000000",
    "imprint_rolling_budget_microusd": "20000000",
    "imprint_monthly_budget_microusd": "20000000",
}


def pytest_addoption(parser):
    parser.addoption(
        "--target",
        choices=("local", "url"),
        default=os.environ.get("CONTRACT_TARGET", "local"),
        help="local: control/ on DynamoDB Local and MinIO with test hooks; "
        "url: an already running origin (CONTRACT_BASE_URL), hook-free tests only",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "hookless: needs no test hooks, so it can run against a deployed origin"
    )


@pytest.fixture(scope="session")
def target(pytestconfig):
    return pytestconfig.getoption("--target")


@pytest.fixture(autouse=True)
def _select(request, target):
    if target == "url" and not request.node.get_closest_marker("hookless"):
        pytest.skip("Needs test hooks")


@pytest.fixture
def runpod():
    fake = FakeRunPod().start()
    yield fake
    fake.stop()


@pytest.fixture
def backend_settings():
    return dict(DEFAULT_SETTINGS)


@pytest.fixture
def backend(target, tmp_path, runpod, backend_settings):
    if target == "url":
        yield None
        return
    service = Backend(tmp_path, runpod.url, backend_settings).start()
    yield service
    service.stop()


@pytest.fixture
def api(target, backend):
    if target == "url":
        return Api(os.environ["CONTRACT_BASE_URL"].rstrip("/"))
    return Api(backend.url)


@pytest.fixture
def headers(api, target):
    if target == "url":
        return {"Authorization": "Bearer " + os.environ["CONTRACT_CLIENT_TOKEN"]}
    return api.register("a")
