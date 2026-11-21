"""Write the JSON Schema of the gateway configuration (docs/config.schema.v1.json).

``python scripts/gen_config_schema.py`` rewrites the committed file; ``--check`` exits 1 when the
committed file is not what the model produces. The unit test runs the same comparison.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp_gateway.config import CONFIG_VERSION, GatewayConfig

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / f"config.schema.v{CONFIG_VERSION}.json"


def render_schema() -> str:
    """The schema as the exact text of the committed file."""
    schema: dict[str, Any] = GatewayConfig.model_json_schema(mode="validation")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"mcp-gateway configuration, version {CONFIG_VERSION}",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render_schema()
    if args.check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != text:
            print(
                f"{SCHEMA_PATH} is stale: run python scripts/gen_config_schema.py", file=sys.stderr
            )
            return 1
        return 0
    SCHEMA_PATH.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
