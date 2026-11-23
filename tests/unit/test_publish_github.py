"""``ci_publish_github.sh`` publishes main and the tags on it, nothing else, and nothing at all when
the history holds a deny-listed string or the target has diverged.

Each test builds a canonical repository (the job's ``origin``), a checkout of it (the job's working
copy) and a bare target standing in for GitHub, reached by path: the SSH options the script sets are
then unused, the selection, the scan and the push are what is tested. Needs git and perl, like the
deny-list tests (the CI job history-denylist-tests runs both files).
"""

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_publish_github.sh"
SECRET = "Zorglub-Industries"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("perl") is None,
    reason="needs git and perl (the CI job history-denylist-tests has both)",
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Dev One",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev One",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.authoring = tmp / "authoring"
        self.origin = tmp / "origin.git"
        self.github = tmp / "github.git"
        self.checkout = tmp / "checkout"
        self.denylist = tmp / "denylist.txt"
        self.key = tmp / "key.b64"
        self.denylist.write_text(f"{SECRET}\n")
        self.key.write_text(base64.b64encode(b"not a real key").decode())
        self.authoring.mkdir()
        git(self.authoring, "init", "-q", "-b", "main")
        self.root = commit(self.authoring, "README.md", "hello\n")
        git(self.authoring, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        git(self.authoring, "checkout", "-q", "-b", "dev")
        self.dev_only = commit(self.authoring, "dev.txt", "work in progress\n")
        git(self.authoring, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        git(self.authoring, "checkout", "-q", "main")
        subprocess.run(["git", "init", "-q", "--bare", str(self.github)], check=True, timeout=30)

    def publish_origin(self) -> None:
        if self.origin.exists():
            shutil.rmtree(self.origin)
        subprocess.run(
            ["git", "clone", "-q", "--mirror", str(self.authoring), str(self.origin)],
            check=True,
            timeout=30,
            env={**os.environ, **GIT_ENV},
        )
        if self.checkout.exists():
            shutil.rmtree(self.checkout)
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.origin), str(self.checkout)],
            check=True,
            timeout=30,
            env={**os.environ, **GIT_ENV},
        )

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        self.publish_origin()
        full = {
            **os.environ,
            **GIT_ENV,
            "GITHUB_PUBLISH_REPO": str(self.github),
            "GITHUB_PUBLISH_DEPLOY_KEY": str(self.key),
            "HISTORY_DENYLIST_FILE": str(self.denylist),
            **env,
        }
        for name, value in env.items():
            if value == "":
                del full[name]
        return subprocess.run(
            ["sh", str(SCRIPT)],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            timeout=60,
            env=full,
        )

    def published(self) -> dict[str, str]:
        out = git(self.github, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines()) if out else {}


@pytest.fixture
def repo(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_publishes_main_and_only_the_tags_on_it(repo: Fixture) -> None:
    result = repo.run()
    assert result.returncode == 0, result.stderr
    refs = repo.published()
    assert set(refs) == {"refs/heads/main", "refs/tags/v0.1.0"}
    assert git(repo.github, "rev-parse", "refs/heads/main") == repo.root
    # The dev branch and the tag cut on dev are not published, nor their commit.
    with pytest.raises(subprocess.CalledProcessError):
        git(repo.github, "cat-file", "-e", repo.dev_only)


def test_a_tag_pipeline_for_a_tag_not_on_main_publishes_nothing(repo: Fixture) -> None:
    # An unprotected tag pipeline has neither protected variable: it must stop before needing them.
    result = repo.run(
        CI_COMMIT_TAG="v0.2.0", GITHUB_PUBLISH_DEPLOY_KEY="", HISTORY_DENYLIST_FILE=""
    )
    assert result.returncode == 0, result.stderr
    assert "not on main" in result.stdout
    assert repo.published() == {}


def test_a_deny_listed_string_on_main_stops_the_publication(repo: Fixture) -> None:
    commit(repo.authoring, "notes.md", f"customer {SECRET}\n")
    result = repo.run()
    assert result.returncode == 1
    # The scan names the pattern, never the line it matched.
    assert f"customer {SECRET}" not in result.stdout + result.stderr
    assert repo.published() == {}


def test_a_string_only_on_dev_does_not_stop_main(repo: Fixture) -> None:
    git(repo.authoring, "checkout", "-q", "dev")
    commit(repo.authoring, "draft.md", f"customer {SECRET}\n")
    git(repo.authoring, "checkout", "-q", "main")
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert set(repo.published()) == {"refs/heads/main", "refs/tags/v0.1.0"}


def test_a_diverged_target_is_refused_not_overwritten(repo: Fixture) -> None:
    other = repo.tmp / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    foreign = commit(other, "foreign.txt", "not ours\n")
    git(other, "push", "-q", str(repo.github), "main:main")
    result = repo.run()
    assert result.returncode != 0
    assert git(repo.github, "rev-parse", "refs/heads/main") == foreign


def test_a_missing_deploy_key_fails_loudly(repo: Fixture) -> None:
    result = repo.run(GITHUB_PUBLISH_DEPLOY_KEY="")
    assert result.returncode == 2
    assert "GITHUB_PUBLISH_DEPLOY_KEY is not set" in result.stderr
    assert repo.published() == {}


def test_a_dry_run_scans_but_does_not_push(repo: Fixture) -> None:
    result = repo.run(PUBLISH_DRY_RUN="true")
    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert repo.published() == {}
