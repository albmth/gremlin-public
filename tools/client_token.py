"""Create a local client credential and print only its non-secret settings entry."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as stream:
        json.dump({"token": token}, stream)
        stream.write("\n")
    print(
        "GREMLIN_CLIENT_TOKEN_HASHES='"
        + json.dumps({args.client: hashlib.sha256(token.encode()).hexdigest()})
        + "'"
    )


if __name__ == "__main__":
    main()
