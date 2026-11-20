"""Turn the JSONL written by ``scripts/loadtest_stats.sh`` into per-container CPU and memory.

Each input line is ``{"ts": <UTC time>, "stats": <one line of docker stats --format json>}``.
The output has, per container, the number of samples and the peak and mean of the CPU percentage
(of one core: 100 means one full core) and of the memory in use (MiB).

    python -m load.stats_summary <stats.jsonl | -> <summary.json | ->
"""

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Binary and decimal units docker prints; the value is the factor to bytes.
_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}
_QUANTITY = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
_MIB = 1024**2


def parse_cpu_percent(text: str) -> float:
    """``"12.34%"`` to ``12.34``."""
    stripped = text.strip()
    if not stripped.endswith("%"):
        raise ValueError(f"not a percentage: {text!r}")
    return float(stripped[:-1])


def parse_memory_mib(usage: str) -> float:
    """The used part of docker's ``"45.6MiB / 7.7GiB"`` in MiB."""
    used = usage.split("/", 1)[0]
    match = _QUANTITY.match(used)
    if match is None:
        raise ValueError(f"not a memory quantity: {usage!r}")
    unit = match.group(2).upper()
    if unit not in _UNITS:
        raise ValueError(f"unknown memory unit {match.group(2)!r} in {usage!r}")
    return float(match.group(1)) * _UNITS[unit] / _MIB


@dataclass(frozen=True)
class ContainerSamples:
    cpu_percent: list[float]
    memory_mib: list[float]


def collect(lines: Iterable[str]) -> dict[str, ContainerSamples]:
    """Group the samples by container name; a malformed line is an error, not skipped."""
    grouped: dict[str, ContainerSamples] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            stats: dict[str, Any] = json.loads(line)["stats"]
            name = stats["Name"]
            cpu = parse_cpu_percent(stats["CPUPerc"])
            memory = parse_memory_mib(stats["MemUsage"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"line {number}: cannot read the stats sample ({exc!r})") from exc
        samples = grouped.setdefault(name, ContainerSamples([], []))
        samples.cpu_percent.append(cpu)
        samples.memory_mib.append(memory)
    return grouped


def summarise(grouped: dict[str, ContainerSamples]) -> dict[str, dict[str, float | int]]:
    return {
        name: {
            "samples": len(samples.cpu_percent),
            "cpu_percent_peak": max(samples.cpu_percent),
            "cpu_percent_mean": sum(samples.cpu_percent) / len(samples.cpu_percent),
            "memory_mib_peak": max(samples.memory_mib),
            "memory_mib_mean": sum(samples.memory_mib) / len(samples.memory_mib),
        }
        for name, samples in sorted(grouped.items())
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise loadtest_stats.sh output.")
    parser.add_argument("input", help="the JSONL file, or - for standard input")
    parser.add_argument("output", help="the JSON file to write, or - for standard output")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    grouped = collect(text.splitlines())
    if not grouped:
        print("no stats sample in the input", file=sys.stderr)
        return 1
    rendered = json.dumps({"containers": summarise(grouped)}, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        Path(args.output).write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
