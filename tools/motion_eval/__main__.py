"""Motion fidelity evaluation of character-replacement candidates against the driving clip.

Run from the repository root:
PYTHONPATH=worker uv run python -m tools.motion_eval COMMAND --out artifacts/motion-eval-<date>
"""

import argparse
import sys
from pathlib import Path

import requests

from tools.hosted_eval.ledger import Hold, campaign_lock

from . import gallery, report, run
from .prepare import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "preflight",
            "probe-transport",
            "approve",
            "run",
            "gallery",
            "report",
            "settle",
        ],
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, default=Path("artifacts/hosted-eval-2026-10-29"))
    parser.add_argument("--max-usd")
    parser.add_argument("--cells")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    root = args.out.resolve()
    repository = Path(__file__).resolve().parents[2]
    if not root.is_relative_to(repository / "artifacts"):
        parser.error("--out must be inside this repository's ignored artifacts directory")
    cells = args.cells.split(",") if args.cells else None
    try:
        with campaign_lock(root):
            if args.command == "prepare":
                prepare(root, args.source_run.resolve())
            elif args.command == "preflight":
                run.preflight(root, cells)
            elif args.command == "probe-transport":
                for config in run.cell_ids(cells):
                    print(config["id"], "HTTP", run.transport_probe(root, config["id"]))
            elif args.command == "approve":
                if not args.max_usd:
                    raise Hold("approve needs --max-usd")
                run.budget(root).approve(args.max_usd)
            elif args.command == "run":
                run.run(root, cells, args.simulate, args.seed)
            elif args.command == "gallery":
                print(gallery.build(root))
            elif args.command == "report":
                print(report.report(root))
            elif args.command == "settle":
                if not args.export:
                    raise Hold("settle needs --export CSV")
                run.settle(root, args.export.resolve())
    except (Hold, requests.RequestException) as error:
        message = (
            str(error)
            if isinstance(error, Hold)
            else "Transport error; reservation retained. Reconcile the provider log first."
        )
        print("HELD: " + message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
