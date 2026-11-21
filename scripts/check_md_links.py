"""Check every relative link of the repository's Markdown files.

A link counts when its target is a path inside the repository: the file or directory must exist and,
for a Markdown target with a ``#fragment``, the fragment must be a heading of that file (GitHub's
slug rules). External URLs, ``mailto:`` links and links inside fenced code blocks are ignored.

    python scripts/check_md_links.py [root]

Exits non-zero and prints ``file:line: target: reason`` for each broken link.
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"(?<!\!)\[(?:[^\]]|\][^(])*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE = re.compile(r"`[^`]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")


@dataclass(frozen=True)
class Broken:
    file: Path
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.target}: {self.reason}"


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, markup dropped, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]", "", text.lower().strip())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else HEADING.match(line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def links(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(INLINE_CODE.sub("", line)):
            found.append((number, match.group(1)))
    return found


def check_file(path: Path, root: Path) -> list[Broken]:
    broken: list[Broken] = []
    for number, target in links(path):
        if EXTERNAL.match(target):
            continue
        raw, _, fragment = target.partition("#")
        destination = path if raw == "" else (path.parent / unquote(raw)).resolve()
        if not destination.is_relative_to(root.resolve()):
            broken.append(Broken(path, number, target, "points outside the repository"))
        elif not destination.exists():
            broken.append(Broken(path, number, target, "no such file or directory"))
        elif fragment and destination.suffix == ".md":
            if unquote(fragment) not in anchors(destination):
                broken.append(Broken(path, number, target, "no such heading"))
    return broken


def markdown_files(root: Path) -> list[Path]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return [root / name for name in listed.split("\0") if name]


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    files = markdown_files(root)
    broken = [item for path in files for item in check_file(path, root)]
    for item in broken:
        print(item)
    print(f"{len(files)} Markdown files, {len(broken)} broken links", file=sys.stderr)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
