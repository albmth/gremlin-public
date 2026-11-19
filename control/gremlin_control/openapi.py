"""Write the published OpenAPI document: `python -m gremlin_control.openapi <path>`.

`docs/openapi.json` is generated from the running code; a test fails when it drifts.
"""

import json
import sys

from gremlin_control.api import create_app
from gremlin_control.settings import Settings


def document():
    schema = create_app(Settings()).openapi()
    schema["components"]["securitySchemes"] = {
        "bearer": {"type": "http", "scheme": "bearer", "description": "Client token"}
    }
    for path, operations in schema["paths"].items():
        if path.startswith("/api/imprint/"):
            for operation in operations.values():
                operation["security"] = [{"bearer": []}]
    return json.dumps(schema, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    with open(sys.argv[1], "w") as stream:
        stream.write(document())
