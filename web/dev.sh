#!/bin/sh
# The website on this machine: local storage, Gremlin (simulated by default) and the site.
set -eu
cd "$(dirname "$0")"
uv sync --frozen --quiet
(cd ../control && uv sync --frozen --quiet)
.venv/bin/python -m devtools.models
# Python itself receives Ctrl-C, so it can stop every server it started.
exec .venv/bin/python -m devtools.local_stack "$@"
