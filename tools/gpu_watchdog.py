"""Terminate an experimental pod once it outlives its time or spending allowance.

Runs on the operator's machine with the service-side provider key read from `.env`, so nothing
inside the pod ever holds a credential. The pod's own hourly price from each read drives the
estimate; the provider's billing read afterwards stays authoritative.
"""

import argparse
import json
import time
from pathlib import Path

import requests

API = "https://api.runpod.io/v2"


def provider_key():
    env = Path(__file__).resolve().parents[1] / ".env"
    for line in env.read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "GREMLIN_IMPRINT_RUNPOD_KEY":
            return value.strip().strip('"').strip("'")
    raise SystemExit("GREMLIN_IMPRINT_RUNPOD_KEY is not set in .env")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--max-minutes", type=float, required=True)
    parser.add_argument("--max-usd", type=float, required=True)
    parser.add_argument(
        "--rate", type=float, help="USD per hour, used only if the pod read carries no cost"
    )
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    headers = {"Authorization": "Bearer " + provider_key()}
    started = time.time()

    def log(**fields):
        print(json.dumps({"at": round(time.time()), **fields}), flush=True)

    while True:
        # A transient provider error must not end the only safety net over a billed pod.
        try:
            response = requests.get(f"{API}/pods/{args.pod}", headers=headers, timeout=30)
            if response.status_code == 404:
                log(event="pod gone")
                return
            response.raise_for_status()
            pod = response.json()
        except requests.RequestException as error:
            log(event="read failed", error=type(error).__name__)
            time.sleep(min(args.interval, 30))
            continue
        rate = pod.get("cost") or args.rate
        if not rate:
            raise SystemExit("The pod read carries no hourly cost; pass --rate")
        minutes = (time.time() - started) / 60
        usd = minutes / 60 * rate
        log(
            minutes=round(minutes, 1),
            usd=round(usd, 3),
            rate=rate,
            status=pod.get("status"),
            cap_minutes=args.max_minutes,
            cap_usd=args.max_usd,
        )
        if minutes >= args.max_minutes or usd >= args.max_usd:
            try:
                delete = requests.delete(f"{API}/pods/{args.pod}", headers=headers, timeout=30)
                log(event="terminated by watchdog", http=delete.status_code)
                time.sleep(15)
                check = requests.get(f"{API}/pods/{args.pod}", headers=headers, timeout=30)
            except requests.RequestException as error:
                log(event="terminate failed, retrying", error=type(error).__name__)
                time.sleep(15)
                continue
            log(event="verified", http=check.status_code, gone=check.status_code == 404)
            if check.status_code == 404:
                return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
