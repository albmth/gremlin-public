"""Build a verified playable comparison using the original named fixture."""

import argparse
import sys
from pathlib import Path

from imprint_worker.review import main as review

from tools.fixtures import load_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=["kungfu", "enzo"], required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _, reference, driving = load_fixture(args.name)
    sys.argv = [
        "review",
        "--reference",
        str(reference),
        "--driving",
        str(driving),
        "--result",
        str(args.result),
        "--output",
        str(args.output),
    ]
    review()


if __name__ == "__main__":
    main()
