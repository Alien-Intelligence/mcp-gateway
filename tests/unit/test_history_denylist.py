"""``ci_history_denylist.sh`` must find a deny-listed string anywhere in the history, fail loudly
when it cannot run, and never print the text it matched.

Each test builds a throwaway repository. They need ``git`` and ``perl`` (the CI job that runs the
script installs git; the generic unit-test image has none, so there the file is skipped and the
job ``history-denylist`` runs it).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_history_denylist.sh"
SECRET = "Zorglub-Industries"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("perl") is None,
    reason="needs git and perl (the CI job history-denylist has both)",
)


def git(repo: Path, *args: str, author: str = "Dev One <dev@example.com>") -> str:
    name, _, email = author.partition(" <")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email.rstrip(">"),
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email.rstrip(">"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=60,
    ).stdout.strip()


def commit(repo: Path, path: str, text: str, message: str = "change", **kwargs: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, **kwargs)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "README.md", "a harmless project\n", "initial")
    return path


def denylist(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "denylist.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def scan(repo: Path, denylist_file: Path | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "HISTORY_DENYLIST_FILE"}
    if denylist_file is not None:
        env["HISTORY_DENYLIST_FILE"] = str(denylist_file)
    return subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


def test_a_clean_history_passes(repo: Path, tmp_path: Path) -> None:
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no hit" in result.stdout


def test_a_planted_string_in_a_file_fails_and_names_the_commit_and_pattern(
    repo: Path, tmp_path: Path
) -> None:
    sha = commit(repo, "src/app.py", f"CUSTOMER = '{SECRET}'\n")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert f"HIT commit {sha[:12]} (diff) pattern: {SECRET}" in result.stdout


def test_the_matched_line_is_never_printed(repo: Path, tmp_path: Path) -> None:
    commit(repo, "src/app.py", f"line before\nprefix {SECRET} suffix-with-more-context\n")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert "suffix-with-more-context" not in result.stdout + result.stderr
    assert "prefix" not in result.stdout + result.stderr


def test_a_string_that_was_deleted_again_is_still_in_the_history(
    repo: Path, tmp_path: Path
) -> None:
    sha = commit(repo, "notes.txt", f"{SECRET}\n")
    commit(repo, "notes.txt", "clean now\n", "remove it")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert sha[:12] in result.stdout


def test_a_commit_message_is_scanned(repo: Path, tmp_path: Path) -> None:
    sha = commit(repo, "a.txt", "clean\n", f"deploy for {SECRET}")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert f"HIT commit {sha[:12]} (header)" in result.stdout


def test_an_author_identity_is_scanned(repo: Path, tmp_path: Path) -> None:
    sha = commit(repo, "a.txt", "clean\n", author="Jane Roe <jane.roe@example.org>")
    result = scan(repo, denylist(tmp_path, r"\bJane Roe\b"))
    assert result.returncode == 1
    assert f"HIT commit {sha[:12]} (header)" in result.stdout


def test_a_commit_on_another_branch_is_scanned(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    sha = commit(repo, "side.txt", f"{SECRET}\n")
    git(repo, "checkout", "-q", "main")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert sha[:12] in result.stdout


def test_a_tag_message_is_scanned(repo: Path, tmp_path: Path) -> None:
    git(repo, "tag", "-a", "v1", "-m", f"release for {SECRET}")
    result = scan(repo, denylist(tmp_path, SECRET))
    assert result.returncode == 1
    assert "ref refs/tags/v1" in result.stdout


def test_patterns_are_perl_regexes_with_their_own_case_rules(repo: Path, tmp_path: Path) -> None:
    commit(repo, "a.txt", "internal host: build.corp.internal.example\n")
    assert scan(repo, denylist(tmp_path, r"corp\.INTERNAL")).returncode == 0
    assert scan(repo, denylist(tmp_path, r"(?i)corp\.INTERNAL")).returncode == 1


def test_blank_lines_and_comments_in_the_list_are_ignored(repo: Path, tmp_path: Path) -> None:
    commit(repo, "a.txt", f"{SECRET}\n")
    result = scan(repo, denylist(tmp_path, "# customers", "", SECRET))
    assert result.returncode == 1
    assert "against 1 patterns" in result.stdout


def test_a_missing_variable_fails_instead_of_passing(repo: Path) -> None:
    result = scan(repo, None)
    assert result.returncode == 2
    assert "HISTORY_DENYLIST_FILE is not set" in result.stderr


def test_a_missing_file_fails(repo: Path, tmp_path: Path) -> None:
    result = scan(repo, tmp_path / "does-not-exist.txt")
    assert result.returncode == 2
    assert "readable file" in result.stderr


def test_an_empty_list_fails_instead_of_checking_nothing(repo: Path, tmp_path: Path) -> None:
    result = scan(repo, denylist(tmp_path, "# only a comment", ""))
    assert result.returncode == 2
    assert "holds no pattern" in result.stderr


def test_an_invalid_pattern_fails_instead_of_weakening_the_scan(repo: Path, tmp_path: Path) -> None:
    commit(repo, "a.txt", f"{SECRET}\n")
    result = scan(repo, denylist(tmp_path, SECRET, "(unclosed"))
    assert result.returncode == 2
    assert "line 2 is not a valid Perl regular expression" in result.stderr


def test_a_directory_that_is_not_a_repository_fails(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    result = scan(empty, denylist(tmp_path, SECRET))
    assert result.returncode == 2
    assert "could not read the history" in result.stderr
