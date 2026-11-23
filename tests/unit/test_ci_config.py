"""The CI pipeline's structure: what must hold for a release image to be trustworthy.

The file is parsed as YAML (GitLab's ``extends`` is resolved by hand for the keys read here), never
searched as text.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
CHART_PIPELINE = yaml.safe_load((ROOT / "ci" / "chart.gitlab-ci.yml").read_text(encoding="utf-8"))
JOBS: dict[str, dict[str, Any]] = {
    name: job
    for source in (PIPELINE, CHART_PIPELINE)
    for name, job in source.items()
    if isinstance(job, dict) and ({"script", "extends", "image"} & job.keys())
}
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def resolved(name: str, key: str) -> Any:
    """The value of ``key`` for a job, following ``extends`` (the job's own value wins)."""
    job = JOBS[name]
    if key in job:
        return job[key]
    parents = job.get("extends", [])
    for parent in [parents] if isinstance(parents, str) else parents:
        if parent in JOBS:
            value = resolved(parent, key)
            if value is not None:
                return value
    return None


def image_names(job: str) -> list[str]:
    names: list[str] = []
    image = resolved(job, "image")
    if image is not None:
        names.append(image["name"] if isinstance(image, dict) else image)
    for service in resolved(job, "services") or []:
        names.append(service["name"] if isinstance(service, dict) else service)
    return names


@pytest.mark.parametrize("job", sorted(JOBS))
def test_every_job_image_is_pinned_by_digest(job: str) -> None:
    for name in image_names(job):
        assert DIGEST.search(name), f"{job}: {name} is not pinned by digest"


def test_the_jobs_that_matter_have_images() -> None:
    """Guards the parametrised test above against passing on an empty set."""
    for job in ("docker-build", "image-scan", "image-publish", "gitleaks", "chart-validate"):
        assert image_names(job), job
    assert any("trivy" in name for name in image_names("image-scan"))
    assert any("dind" in name for name in image_names("docker-build"))


def test_the_tags_are_applied_only_after_the_scan_passed() -> None:
    needs = JOBS["image-publish"]["needs"]
    assert set(needs) == {"docker-build", "image-scan"}
    assert JOBS["image-scan"]["needs"] == ["docker-build"]
    assert resolved("image-publish", "script") == [
        "set -a && . ./build.env && set +a",
        "sh scripts/ci_image_publish.sh",
    ]


def test_the_build_job_runs_the_script_that_pushes_by_digest() -> None:
    assert JOBS["docker-build"]["script"] == ["sh scripts/ci_image_build.sh"]
    script = (ROOT / "scripts" / "ci_image_build.sh").read_text(encoding="utf-8")
    assert "push-by-digest=true" in script
    assert "--tag" not in script
    assert "imagetools create" not in script


def test_the_release_hold_rules_still_come_from_the_shared_job() -> None:
    """Redeclaring the jobs must not replace the shared job's `rules:` (the post-merge release hold)."""
    for name in ("docker-build", "image-scan", "image-publish"):
        assert "rules" not in JOBS[name], f"{name} must inherit the shared rules, not set its own"
