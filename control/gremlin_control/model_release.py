"""The two supported weight sources and their immutable release contract."""

import json
import re


def valid_release(value, source):
    if (
        not isinstance(source, str)
        or source not in {"runtime-hf", "native-cache"}
        or not isinstance(value, dict)
        or set(value) != {"version", "asset_set_sha256", "repo", "revision"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["asset_set_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["asset_set_sha256"])
    ):
        return False
    if source == "runtime-hf":
        return value["repo"] is None and value["revision"] is None
    repo, revision = value["repo"], value["revision"]
    return bool(
        isinstance(repo, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}", repo)
        and ".." not in repo
        and "--" not in repo
        and isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", revision)
    )


def configured_release(endpoint):
    environment = endpoint.get("env", {})
    source = environment.get("IMPRINT_WEIGHT_SOURCE")
    raw = environment.get("IMPRINT_MODEL_RELEASE")
    if source is None and raw is None:
        return None, None
    try:
        release = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Worker model release configuration is invalid") from None
    if not valid_release(release, source):
        raise ValueError("Worker model release configuration is invalid")
    return source, release


def model_references(source, release):
    if source == "native-cache":
        return [f"https://huggingface.co/{release['repo']}:{release['revision']}"]
    return []
