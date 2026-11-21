"""Check the gateway configuration a chart render carries, with the gateway's own loader.

Reads a rendered manifest stream on stdin (``helm template`` output), takes the
``MCP_GATEWAY_CONFIG`` key of the Secret the chart renders from ``config`` and runs it through
``mcp_gateway.config.parse_config``: the function the gateway runs at start. What the loader
refuses here, the gateway would refuse in the pod. The loader reads no file at parse time, so the
``*_file`` paths of a test configuration need not exist.

Usage: chart_config_check.py [--expect-refused TEXT | --expect-none]

  (default)               exactly one such Secret must be present and its document accepted
  --expect-refused TEXT   the document must be refused, and the refusal must contain TEXT
  --expect-none           no Secret with the configuration may be present (existingSecret)

Exit status 0 when the expectation holds, 1 when it does not.
"""

import argparse
import base64
import binascii
import re
import sys

from mcp_gateway.config import CONFIG_ENV_VAR, ConfigError, parse_config

# The chart writes the key as `  MCP_GATEWAY_CONFIG: <base64>` (templates/secret.yaml).
_DATA_LINE = re.compile(rf"^  {CONFIG_ENV_VAR}: (?P<value>\S+)$", re.MULTILINE)


def configs_in(manifests: str) -> list[str]:
    """The decoded configuration documents of the Secrets in a rendered manifest stream."""
    documents: list[str] = []
    for manifest in re.split(r"^---\s*$", manifests, flags=re.MULTILINE):
        if not re.search(r"^kind: Secret\s*$", manifest, flags=re.MULTILINE):
            continue
        for match in _DATA_LINE.finditer(manifest):
            try:
                documents.append(base64.b64decode(match["value"], validate=True).decode("utf-8"))
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise SystemExit(
                    f"FAIL: {CONFIG_ENV_VAR} is not base64 of UTF-8 text: {exc}"
                ) from exc
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the gateway configuration of a chart render through the gateway's loader."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--expect-refused", metavar="TEXT")
    group.add_argument("--expect-none", action="store_true")
    args = parser.parse_args()

    documents = configs_in(sys.stdin.read())
    if args.expect_none:
        if documents:
            print(
                f"FAIL: expected no {CONFIG_ENV_VAR} Secret, found {len(documents)}",
                file=sys.stderr,
            )
            return 1
        print(f"no {CONFIG_ENV_VAR} Secret rendered, as expected")
        return 0
    if len(documents) != 1:
        print(
            f"FAIL: expected exactly one {CONFIG_ENV_VAR} Secret, found {len(documents)}",
            file=sys.stderr,
        )
        return 1
    try:
        parse_config(documents[0])
    except ConfigError as exc:
        if args.expect_refused is not None and args.expect_refused in str(exc):
            print(f"refused as expected: {exc}")
            return 0
        print(f"FAIL: the gateway would refuse this configuration: {exc}", file=sys.stderr)
        return 1
    if args.expect_refused is not None:
        print(
            f'FAIL: the configuration was accepted, "{args.expect_refused}" was expected',
            file=sys.stderr,
        )
        return 1
    print("configuration accepted by the gateway's loader")
    return 0


if __name__ == "__main__":
    sys.exit(main())
