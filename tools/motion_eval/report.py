"""Markdown summary of measurements, costs and latency for the handoff."""

import json

from tools.hosted_eval.ledger import Ledger

from .run import budget


def table(rows, columns):
    lines = [
        "| " + " | ".join(c[0] for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c[1](row)) for c in columns) + " |")
    return "\n".join(lines)


def fmt(value, digits=2):
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def report(root):
    results = json.loads((root / "measurements.json").read_text())
    ledger = Ledger(root)
    money = budget(root)
    lines = [f"# Motion fidelity evaluation: {root.name}", ""]
    cap = (
        json.loads((root / "budget.json").read_text())["cap_usd"]
        if (root / "budget.json").exists()
        else "none"
    )
    lines.append(
        f"Committed (conservative) USD {money.total()}, likely USD {money.likely_total()}, "
        f"cap USD {cap}."
    )
    lines.append("")
    for fixture in ("kungfu", "enzo"):
        rows = [dict(key=k, **v) for k, v in results.items() if v["fixture"] == fixture]
        lines += [f"## {'Kung Fu' if fixture == 'kungfu' else 'Enzo'}", ""]
        lines.append(
            table(
                rows,
                [
                    ("Clip", lambda r: r["title"]),
                    ("Kind", lambda r: "baseline" if r["baseline"] else "candidate"),
                    ("Background PSNR dB", lambda r: fmt(r["fidelity"]["background_psnr_db"], 1)),
                    ("Subject flow cosine", lambda r: fmt(r["fidelity"]["subject_flow_cosine"])),
                    ("Subject flow error", lambda r: fmt(r["fidelity"]["subject_flow_error"])),
                    ("Timing r", lambda r: fmt(r["fidelity"]["motion_timing_r"])),
                    ("Energy ratio", lambda r: fmt(r["fidelity"]["subject_energy_ratio"])),
                    ("List USD", lambda r: fmt((r.get("cost") or {}).get("list_usd"), 4)),
                    ("Posted USD", lambda r: fmt((r.get("cost") or {}).get("posted_usd"), 4)),
                    ("Queue s", lambda r: fmt((r.get("latency") or {}).get("queue_s"), 1)),
                    ("Inference s", lambda r: fmt((r.get("latency") or {}).get("inference_s"), 1)),
                    ("Total s", lambda r: fmt((r.get("latency") or {}).get("total_s"), 1)),
                ],
            )
        )
        lines.append("")
    failures = [e for e in ledger.select("terminal") if e["status"] != "succeeded"]
    if failures:
        lines += ["## Provider failures and holds", ""]
        for event in failures:
            lines.append(f"- {event['cell']}: {event['status']} ({event.get('reason', '')})")
        lines.append("")
    (root / "report.md").write_text("\n".join(lines))
    return root / "report.md"
