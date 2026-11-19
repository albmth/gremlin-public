"""Validate every recipe's graphs against a live engine's node contracts without loading weights."""

import argparse
import json
from pathlib import Path

import requests

from .graph import generation_graph, tracking_graph, validate_graph
from .recipe import load, recipe_paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    response = requests.get(args.url + "/object_info", timeout=60)
    response.raise_for_status()
    schemas = response.json()
    graphs = [tracking_graph("schema-check", {"x": 256, "y": 256}, {"x": 256, "y": 256})]
    for path in recipe_paths():
        recipe = load(path)
        graphs.append(
            generation_graph("schema-check", 512, 512, 81, 42, "schema check prompt", recipe)
        )
    for graph in graphs:
        validate_graph(graph, schemas)
    attention = schemas["ModelAttentionBackend"]["input"]["required"]["attention"][1]
    args.output.write_text(
        json.dumps(
            {
                "graphs_validated": len(graphs),
                "native_nodes_validated": sum(map(len, graphs)),
                "attention_backends": attention.get("options", []),
                "gpu_inference_executed": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(args.output.read_text())


if __name__ == "__main__":
    main()
