"""The runtime image must not let its own user rewrite the application code."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def test_the_application_venv_is_not_chowned_to_the_runtime_user() -> None:
    copies = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if line.startswith("COPY") and "/app/.venv" in line
    ]
    assert copies, "the runtime stage must copy the venv from the builder"
    for line in copies:
        assert not re.search(r"--chown", line), f"venv must stay root-owned: {line}"
