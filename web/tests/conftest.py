import os
import uuid

import pytest
from devtools import local_stack, models

from tests import media as synthetic

os.environ.update(local_stack.EMULATORS)
STORAGE = local_stack.TEST_STORAGE
# Enough for a handful of local stand-in calls (1685 micro-USD settled, 6200 reserved each).
BUDGET = {"caption_daily_microusd": 40_000, "caption_monthly_microusd": 500_000}


@pytest.fixture(scope="session", autouse=True)
def detector():
    """The worker's pinned detector weights, fetched once and verified (never committed)."""
    return models.ensure()


@pytest.fixture(scope="session")
def workdir(tmp_path_factory):
    if not local_stack.storage_up(STORAGE):
        pytest.skip("DynamoDB Local and MinIO are not running (see contract/compose.yaml)")
    return tmp_path_factory.mktemp("web")


class Site(str):
    """A running site's origin, with its table name for reading the ledger directly."""

    table: str
    gremlin_url: str
    gremlin_token: str


def _stack(request, workdir, mode, **settings):
    name = "gremlin-web-test-" + uuid.uuid4().hex[:12]
    settings = local_stack.UNLIMITED | settings
    gremlin, gremlin_url, token = local_stack.gremlin(
        workdir / f"gremlin-{name}.log", mode, STORAGE
    )
    web, origin = local_stack.web(
        workdir / f"web-{name}.log", gremlin_url, token, STORAGE, name=name, **settings
    )
    request.addfinalizer(gremlin.stop)
    request.addfinalizer(web.stop)
    site = Site(origin)
    site.table, site.gremlin_url, site.gremlin_token = name, gremlin_url, token
    return site


def _caption(model):
    return {"caption_model": model} | BUDGET


@pytest.fixture(scope="session")
def site(request, workdir):
    """The website on a Gremlin whose simulated worker delivers every job."""
    return _stack(request, workdir, "succeed", **_caption("local"))


@pytest.fixture(scope="session")
def rejecting_site(request, workdir):
    """The website on a Gremlin whose simulated worker rejects every job's inputs on the GPU."""
    return _stack(request, workdir, "reject", **_caption("local"))


@pytest.fixture(scope="session")
def limited_site(request, workdir):
    """The website with small public quotas, to see them hold."""
    return _stack(
        request,
        workdir,
        "succeed",
        total_generations=2,
        address_hourly_drafts=6,
        **_caption("local"),
    )


@pytest.fixture(scope="session")
def fixed_site(request, workdir):
    return _stack(request, workdir, "succeed", caption_model="fixed")


@pytest.fixture(scope="session")
def caption_sites(request, workdir, fixed_site):
    """Sites whose caption stand-in doubts automatic choices, answers unusably or never answers,
    flags a public figure, one with no caption budget at all, and one that sends the fixed
    description without any."""
    return {
        "unclear": _stack(request, workdir, "succeed", **_caption("local-unclear")),
        "public": _stack(request, workdir, "succeed", **_caption("local-public-figure")),
        "malformed": _stack(request, workdir, "succeed", **_caption("local-malformed")),
        "silent": _stack(request, workdir, "succeed", **_caption("local-silent")),
        "closed": _stack(request, workdir, "succeed", caption_model="local"),
        "fixed": fixed_site,
    }


@pytest.fixture(scope="session")
def media():
    """{name: bytes}: photos and clips, synthetic and real."""
    person = synthetic.png(synthetic.scene([(0.5, 0.8)]))
    return {
        "reference": person,
        "two_people": synthetic.png(synthetic.scene([(0.3, 0.75), (0.7, 0.75)])),
        "driving": synthetic.clip(synthetic.CLIPS["one"]),
        "two_clip": synthetic.clip(synthetic.CLIPS["two"]),
        "leaves_clip": synthetic.clip(synthetic.CLIPS["leaves"]),
        "short_clip": synthetic.clip(synthetic.CLIPS["one"], seconds=2.5),
        "screenshot": synthetic.fixture("Screenshot").read_bytes(),
        "kungfu": synthetic.fixture("Kung Fu").read_bytes(),
        "enzo": synthetic.fixture("Enzo").read_bytes(),
    }
