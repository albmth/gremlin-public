"""Preserve compatibility with the recovered pinned engine's actual socket contracts."""

import json
from pathlib import Path

from gremlin_control.limits import TIERS
from imprint_worker.graph import generation_graph, tracking_graph, validate_graph
from imprint_worker.recipe import TIER_RECIPES, load, recipe_paths

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMAS = json.loads((FIXTURES / "comfy-node-schemas.json").read_text())


def canonical(graph):
    """Node ids are labels; the contract is each node's class and its resolved inputs."""

    def form(name):
        inputs = {}
        for key, value in graph[name]["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                value = [form(value[0]), value[1]]
            inputs[key] = value
        return [graph[name]["class_type"], inputs]

    return sorted(json.dumps(form(name), sort_keys=True) for name in graph)


def test_baseline_graphs_match_recorded_native_engine_contract():
    validate_graph(tracking_graph("test", {"x": 256, "y": 256}, {"x": 256, "y": 256}), SCHEMAS)


def test_reference_recipe_reproduces_the_accepted_generation_graph():
    accepted = json.loads((FIXTURES / "accepted-generation-graph.json").read_text())
    prefix = accepted["ref"]["inputs"]["image"].removesuffix("-masked-reference.png")
    shape = accepted["condition"]["inputs"]
    built = generation_graph(
        prefix,
        shape["width"],
        shape["height"],
        shape["length"],
        accepted["sample"]["inputs"]["seed"],
        accepted["positive"]["inputs"]["text"],
    )
    assert canonical(built) == canonical(accepted)


def test_the_service_accepts_exactly_the_tiers_the_worker_can_run():
    # The API and the worker deploy separately; a tier missing on either side would only
    # fail on a paid GPU after the cold start.
    assert sorted(TIERS) == sorted(TIER_RECIPES)
    for name in TIERS:
        assert load(TIER_RECIPES[name])["assets"]


def test_every_recipe_matches_the_engine_contract():
    for path in recipe_paths():
        graph = generation_graph("test", 512, 512, 81, 42, "test prompt", load(path))
        validate_graph(graph, SCHEMAS)
