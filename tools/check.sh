#!/bin/sh
# All checks run locally; no service or GPU credentials are needed.
# The service is checked on DynamoDB Local and MinIO, so Docker must be running.
set -eu
cd "$(dirname "$0")/.."
uv run python tools/check_docs.py
uv run pytest -q
docker compose -f contract/compose.yaml up -d --quiet-pull
for port in 8001 9100; do
    tries=0
    until curl -s -o /dev/null "http://127.0.0.1:$port"; do
        tries=$((tries + 1))
        [ "$tries" -lt 60 ] || { echo "emulator on port $port did not start" >&2; exit 1; }
        sleep 0.5
    done
done
uv run pytest contract -q
(cd control && uv run --frozen pytest -q -p no:cacheprovider)
(cd web && uv run --frozen pytest -q -p no:cacheprovider)
uv run ruff check
uv run ruff format --check
git diff --check
git diff --cached --check
