"""The chart checks must fail when the render fails, and read the render as YAML.

``chart_check.sh`` is run here with stand-ins for ``helm`` and ``kubeconform`` on the PATH, so the
tests need neither binary: what they prove is the script's handling of a failing render.
"""

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from chart_config_check import (
    RenderError,
    check_inline,
    check_none,
    config_secrets_in,
    deployment_config_ref,
    manifests_in,
)

from mcp_gateway.config import CONFIG_ENV_VAR

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "chart_check.sh"
CHECKER = ROOT / "scripts" / "chart_config_check.py"


def secret(name: str, document: str) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "data": {CONFIG_ENV_VAR: base64.b64encode(document.encode()).decode()},
    }


def deployment(secret_name: str | None, key: str = CONFIG_ENV_VAR) -> dict[str, object]:
    env: list[dict[str, object]] = []
    if secret_name is not None:
        env.append(
            {
                "name": CONFIG_ENV_VAR,
                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
            }
        )
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "gateway"},
        "spec": {"template": {"spec": {"containers": [{"name": "gateway", "env": env}]}}},
    }


def stream(*manifests: dict[str, object]) -> str:
    return yaml.safe_dump_all(manifests)


def run_checker(render: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(CHECKER), *args],
        input=render,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        timeout=60,
    )


def test_a_comment_or_a_string_that_looks_like_a_secret_is_not_one() -> None:
    render = (
        "# kind: Secret\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n  name: x\n"
        "data:\n"
        "  note: |\n"
        "    kind: Secret\n"
        f"  {CONFIG_ENV_VAR}: bm90LWEtc2VjcmV0\n"
    )
    assert config_secrets_in(list(manifests_in(render))) == []


def test_a_render_that_is_not_yaml_is_an_error() -> None:
    with pytest.raises(RenderError, match="not valid YAML"):
        list(manifests_in("a: [unclosed\n"))


def test_the_checker_fails_on_an_empty_render() -> None:
    result = run_checker("")
    assert result.returncode == 1
    assert "render is empty" in result.stderr


def test_expect_none_passes_when_the_deployment_reads_the_existing_secret() -> None:
    check_none([deployment("mcp-gateway-config")], "mcp-gateway-config", CONFIG_ENV_VAR)


def test_expect_none_fails_when_the_deployment_reads_another_secret() -> None:
    with pytest.raises(RenderError, match="expected the existing Secret"):
        check_none([deployment("other")], "mcp-gateway-config", CONFIG_ENV_VAR)


def test_expect_none_fails_when_the_deployment_reads_the_wrong_key() -> None:
    with pytest.raises(RenderError, match="expected the existing Secret"):
        check_none([deployment("mcp-gateway-config", key="x")], "mcp-gateway-config", "y")


def test_expect_none_fails_when_the_deployment_has_no_config_variable() -> None:
    with pytest.raises(RenderError, match="found 0"):
        check_none([deployment(None)], "mcp-gateway-config", CONFIG_ENV_VAR)


def test_expect_none_fails_when_there_is_no_deployment() -> None:
    with pytest.raises(RenderError, match="exactly one Deployment"):
        check_none([], "mcp-gateway-config", CONFIG_ENV_VAR)


def test_expect_none_fails_when_an_inline_secret_is_rendered() -> None:
    with pytest.raises(RenderError, match="expected no"):
        check_none(
            [secret("s", "{}"), deployment("mcp-gateway-config")],
            "mcp-gateway-config",
            CONFIG_ENV_VAR,
        )


def test_expect_none_through_the_command_line() -> None:
    good = stream(deployment("mcp-gateway-config"))
    args = ("--expect-none", "--secret-name", "mcp-gateway-config", "--secret-key", CONFIG_ENV_VAR)
    assert run_checker(good, *args).returncode == 0
    assert run_checker(stream(deployment("elsewhere")), *args).returncode == 1


def test_inline_configuration_must_be_the_one_the_deployment_reads() -> None:
    document = json.dumps({"version": 1})
    assert check_inline([secret("s", document), deployment("s")]) == document
    with pytest.raises(RenderError, match="not the rendered Secret"):
        check_inline([secret("s", document), deployment("elsewhere")])


def test_inline_configuration_needs_exactly_one_secret() -> None:
    with pytest.raises(RenderError, match="found 0"):
        check_inline([deployment("s")])


def test_a_secret_with_a_key_that_is_not_base64_is_an_error() -> None:
    broken = secret("s", "x")
    broken["data"] = {CONFIG_ENV_VAR: "***"}
    with pytest.raises(RenderError, match="not base64"):
        config_secrets_in([broken])


def test_the_deployment_reference_must_be_complete() -> None:
    incomplete = deployment("s")
    incomplete["spec"] = {
        "template": {
            "spec": {
                "containers": [
                    {"env": [{"name": CONFIG_ENV_VAR, "valueFrom": {"secretKeyRef": {}}}]}
                ]
            }
        }
    }
    with pytest.raises(RenderError, match="not a full secretKeyRef"):
        deployment_config_ref([incomplete])


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def standins(tmp_path: Path) -> dict[str, Path]:
    """A chart directory with one values file and stand-ins for helm and kubeconform."""
    chart = tmp_path / "chart"
    (chart / "ci").mkdir(parents=True)
    (chart / "ci" / "existing-secret-values.yaml").write_text("x: 1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "kubeconform", "#!/bin/sh\nexit 0\n")
    return {"chart": chart, "bin": bin_dir}


def run_chart_check(standins: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(CHECK)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{standins['bin']}{os.pathsep}{os.environ['PATH']}",
            "CHART_DIR": str(standins["chart"]),
        },
        timeout=300,
    )


def test_chart_check_fails_when_helm_template_fails(standins: dict[str, Path]) -> None:
    """The defect: helm's failure was lost in a pipe and --expect-none then passed on nothing."""
    write_executable(
        standins["bin"] / "helm",
        '#!/bin/sh\nif [ "$1" = "template" ]; then echo "boom: template failed" >&2; exit 1; fi\nexit 0\n',
    )
    result = run_chart_check(standins)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL: helm template existing-secret-values.yaml" in result.stderr
    assert "no MCP_GATEWAY_CONFIG Secret rendered" not in result.stdout


def test_chart_check_passes_a_render_whose_deployment_reads_the_existing_secret(
    standins: dict[str, Path],
) -> None:
    render = stream(deployment("mcp-gateway-config"))
    write_executable(
        standins["bin"] / "helm",
        f'#!/bin/sh\nif [ "$1" = "template" ]; then cat <<\'EOF\'\n{render}EOF\nfi\nexit 0\n',
    )
    result = run_chart_check(standins)
    # The only values file renders; the expected-failure files are absent from the temp chart, so
    # the failure table's own entries are the sole source of non-zero status here.
    assert "no MCP_GATEWAY_CONFIG Secret rendered, the Deployment reads" in result.stdout
    assert "FAIL: gateway configuration" not in result.stderr
