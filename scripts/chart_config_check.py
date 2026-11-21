"""Check the gateway configuration a chart render carries, with the gateway's own loader.

Reads a rendered manifest stream on stdin (``helm template`` output), parses it as YAML, takes the
``MCP_GATEWAY_CONFIG`` key of the Secret the chart renders from ``config`` and runs it through
``mcp_gateway.config.parse_config``: the function the gateway runs at start. What the loader
refuses here, the gateway would refuse in the pod. The loader reads no file at parse time, so the
``*_file`` paths of a test configuration need not exist.

The Deployment must also read that Secret: its ``MCP_GATEWAY_CONFIG`` variable must be a
``secretKeyRef`` to the rendered Secret and its key.

Usage: chart_config_check.py [--expect-refused TEXT | --expect-none --secret-name NAME --secret-key KEY]

  (default)               exactly one such Secret must be present, the Deployment must read it, and
                          its document must be accepted
  --expect-refused TEXT   the document must be refused, and the refusal must contain TEXT
  --expect-none           no Secret with the configuration may be rendered, and the Deployment must
                          read the existing Secret NAME, key KEY (existingSecret)

Exit status 0 when the expectation holds, 1 when it does not.
"""

import argparse
import base64
import binascii
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import yaml

from mcp_gateway.config import CONFIG_ENV_VAR, ConfigError, parse_config


class RenderError(Exception):
    """The render is not what the chart promises (not YAML, no Deployment, a wrong reference)."""


@dataclass(frozen=True)
class SecretRef:
    """A ``secretKeyRef``: the Secret a container variable is read from."""

    name: str
    key: str


@dataclass(frozen=True)
class ConfigSecret:
    """A rendered Secret that carries the configuration document."""

    name: str
    document: str


def manifests_in(stream: str) -> Iterator[dict[str, Any]]:
    """The manifests of a rendered stream: every non-empty YAML document must be a mapping."""
    try:
        for manifest in yaml.safe_load_all(stream):
            if manifest is None:
                continue
            if not isinstance(manifest, dict):
                raise RenderError(f"a YAML document of the render is not a mapping: {manifest!r}")
            yield manifest
    except yaml.YAMLError as exc:
        raise RenderError(f"the render is not valid YAML: {exc}") from exc


def config_secrets_in(manifests: list[dict[str, Any]]) -> list[ConfigSecret]:
    """The Secrets that carry the configuration key, with the document decoded."""
    secrets: list[ConfigSecret] = []
    for manifest in manifests:
        if manifest.get("kind") != "Secret":
            continue
        data = manifest.get("data") or {}
        if CONFIG_ENV_VAR not in data:
            continue
        name = (manifest.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name:
            raise RenderError(f"a Secret with {CONFIG_ENV_VAR} has no name")
        try:
            document = base64.b64decode(data[CONFIG_ENV_VAR], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError) as exc:
            raise RenderError(f"{CONFIG_ENV_VAR} is not base64 of UTF-8 text: {exc}") from exc
        secrets.append(ConfigSecret(name=name, document=document))
    return secrets


def deployment_config_ref(manifests: list[dict[str, Any]]) -> SecretRef:
    """The secretKeyRef the single Deployment reads ``MCP_GATEWAY_CONFIG`` from."""
    deployments = [m for m in manifests if m.get("kind") == "Deployment"]
    if len(deployments) != 1:
        raise RenderError(f"expected exactly one Deployment, found {len(deployments)}")
    pod_spec = (((deployments[0].get("spec") or {}).get("template") or {}).get("spec")) or {}
    refs: list[SecretRef] = []
    for container in pod_spec.get("containers") or []:
        for variable in container.get("env") or []:
            if variable.get("name") != CONFIG_ENV_VAR:
                continue
            ref = ((variable.get("valueFrom") or {}).get("secretKeyRef")) or {}
            name, key = ref.get("name"), ref.get("key")
            if not (isinstance(name, str) and name and isinstance(key, str) and key):
                raise RenderError(f"the Deployment's {CONFIG_ENV_VAR} is not a full secretKeyRef")
            refs.append(SecretRef(name=name, key=key))
    if len(refs) != 1:
        raise RenderError(
            f"expected exactly one {CONFIG_ENV_VAR} variable in the Deployment, found {len(refs)}"
        )
    return refs[0]


def check_none(manifests: list[dict[str, Any]], secret_name: str, secret_key: str) -> None:
    """existingSecret: no inline configuration, and the Deployment reads the named Secret."""
    rendered = config_secrets_in(manifests)
    if rendered:
        raise RenderError(f"expected no {CONFIG_ENV_VAR} Secret, found {len(rendered)}")
    ref = deployment_config_ref(manifests)
    if ref != SecretRef(name=secret_name, key=secret_key):
        raise RenderError(
            f"the Deployment reads {ref.name}/{ref.key}, expected the existing Secret "
            f"{secret_name}/{secret_key}"
        )


def check_inline(manifests: list[dict[str, Any]]) -> str:
    """Inline configuration: one Secret, read by the Deployment; returns its document."""
    rendered = config_secrets_in(manifests)
    if len(rendered) != 1:
        raise RenderError(f"expected exactly one {CONFIG_ENV_VAR} Secret, found {len(rendered)}")
    ref = deployment_config_ref(manifests)
    if ref != SecretRef(name=rendered[0].name, key=CONFIG_ENV_VAR):
        raise RenderError(
            f"the Deployment reads {ref.name}/{ref.key}, not the rendered Secret "
            f"{rendered[0].name}/{CONFIG_ENV_VAR}"
        )
    return rendered[0].document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the gateway configuration of a chart render through the gateway's loader."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--expect-refused", metavar="TEXT")
    group.add_argument("--expect-none", action="store_true")
    parser.add_argument("--secret-name", help="with --expect-none: the existing Secret's name")
    parser.add_argument("--secret-key", help="with --expect-none: the key in the existing Secret")
    args = parser.parse_args()
    if args.expect_none and not (args.secret_name and args.secret_key):
        parser.error("--expect-none needs --secret-name and --secret-key")

    stream = sys.stdin.read()
    if not stream.strip():
        print("FAIL: the render is empty", file=sys.stderr)
        return 1
    try:
        manifests = list(manifests_in(stream))
        if args.expect_none:
            check_none(manifests, args.secret_name, args.secret_key)
            print(
                f"no {CONFIG_ENV_VAR} Secret rendered, the Deployment reads "
                f"{args.secret_name}/{args.secret_key}, as expected"
            )
            return 0
        document = check_inline(manifests)
    except RenderError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    try:
        parse_config(document)
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
