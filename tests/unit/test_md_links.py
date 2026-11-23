"""Every relative link in the repository's Markdown files resolves, and the checker can fail."""

from pathlib import Path

from check_md_links import check_file, main, markdown_files, slug

ROOT = Path(__file__).resolve().parents[2]


def test_every_relative_link_in_the_repository_resolves() -> None:
    broken = [str(item) for path in markdown_files(ROOT) for item in check_file(path, ROOT)]
    assert broken == []


def test_the_main_entry_point_agrees() -> None:
    assert main(["check_md_links.py", str(ROOT)]) == 0


def test_a_broken_file_a_broken_heading_and_an_escape_are_reported(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Real heading\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md#real-heading) [file](missing.md) [heading](other.md#nope)\n"
        "[escape](../outside.md) [web](https://example.com/x) [mail](mailto:a@example.com)\n"
        "`[code](nowhere.md)`\n"
        "```\n[fenced](nowhere.md)\n```\n",
        encoding="utf-8",
    )
    reasons = {item.target: item.reason for item in check_file(page, tmp_path)}
    assert reasons == {
        "missing.md": "no such file or directory",
        "other.md#nope": "no such heading",
        "../outside.md": "points outside the repository",
    }


def test_slug_follows_github() -> None:
    assert slug("`policies.rate_limits`") == "policiesrate_limits"
    assert slug("5.4 docker compose") == "54-docker-compose"
    assert slug("CIMD (experimental)") == "cimd-experimental"
