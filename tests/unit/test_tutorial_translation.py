"""The translations of the setup tutorial must not drift from the English chapters.

Code, commands, configuration and the marker comments that tell ``test_tutorial_examples`` what a
fence is are never translated: for each chapter of each translation, the sequence of fenced blocks
(with its info string and the marker comment before it) is the English one, byte for byte.
"""

import re
from pathlib import Path

import pytest

TUTORIAL = Path(__file__).resolve().parents[2] / "docs" / "tutorial"
SOURCE = "en"
TRANSLATIONS = ("fr",)
CHAPTERS = sorted(path.name for path in (TUTORIAL / SOURCE).glob("*.md"))
MARKER = re.compile(r"<!-- [^>]* -->")


def fenced_blocks(path: Path) -> list[str]:
    """Every fence of a chapter, as written, preceded by the marker comment that labels it."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("```"):
            index += 1
            continue
        end = next(i for i in range(index + 1, len(lines)) if lines[i].startswith("```"))
        marker = lines[index - 1] if index and MARKER.fullmatch(lines[index - 1].strip()) else ""
        blocks.append("\n".join([marker, *lines[index : end + 1]]))
        index = end + 1
    return blocks


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_a_translation_has_the_chapters_of_the_source(language: str) -> None:
    assert sorted(path.name for path in (TUTORIAL / language).glob("*.md")) == CHAPTERS


@pytest.mark.parametrize("chapter", CHAPTERS)
@pytest.mark.parametrize("language", TRANSLATIONS)
def test_a_translated_chapter_has_the_code_blocks_of_the_source(
    language: str, chapter: str
) -> None:
    source = fenced_blocks(TUTORIAL / SOURCE / chapter)
    translated = fenced_blocks(TUTORIAL / language / chapter)
    assert len(translated) == len(source), f"{language}/{chapter}: number of fences differs"
    for position, (expected, found) in enumerate(zip(source, translated, strict=True), 1):
        assert found == expected, f"{language}/{chapter}: fence {position} differs from {SOURCE}"
