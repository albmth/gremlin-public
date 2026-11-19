# Working on Gremlin

Read `.agent/project-memory/00-index.md` first.

Gremlin is the generation API in `control/`, the GPU worker in `worker/`, the black-box contract suite in `contract/` and the public demo site in `web/`.
The site is just a client of the API; nothing in `control/` or `worker/` may depend on it.

- Run `./tools/check.sh` before committing (Docker must be running); the pre-commit hook runs it too.
- Change the API only together with `docs/api.md`, the contract suite and the regenerated `docs/openapi.json` (`cd control && uv run python -m gremlin_control.openapi ../docs/openapi.json`); a test fails when they drift.
- Keep credentials, databases, generated media, model weights and the private fixture media out of Git; `artifacts/`, `stickers/`, `testmedia/`, `.env` and `infra/aws/.state/` are ignored.
- Deploying, pushing, changing AWS, IAM or SSM, and anything that runs a paid GPU need the owner's explicit go-ahead each time; dispatch is off and spending authority is zero by default.
- The accepted fixture outputs are the quality reference; any change to generated or restored frames is a paired comparison the owner reviews as video before it ships, and a passing metric is not acceptance.
- Report genuine inference separately from simulated-worker and transport tests.
- Write docs short, casual and technical; one sentence per line in Markdown, no em dashes.
- Read `PLAN.md` before resuming unfinished work.
