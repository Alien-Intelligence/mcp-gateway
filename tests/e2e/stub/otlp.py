"""Reads what the test collector wrote (OTLP JSON, one document per line) into plain Python.

The collector's file exporter marshals the OTLP protobuf messages as JSON: attribute values are
``{"stringValue": ...}``, ``{"intValue": "<digits>"}`` and so on, and 64-bit integers are strings.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRACES_FILE = "traces.jsonl"
METRICS_FILE = "metrics.jsonl"
POLL_SECONDS = 0.5

type Attributes = dict[str, Any]


def attribute_value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "boolValue" in value:
        return value["boolValue"]
    if "doubleValue" in value:
        return float(value["doubleValue"])
    raise AssertionError(f"unexpected attribute value shape: {sorted(value)}")


def attributes_of(entries: list[dict[str, Any]] | None) -> Attributes:
    return {entry["key"]: attribute_value(entry["value"]) for entry in entries or []}


def read_documents(path: Path) -> list[dict[str, Any]]:
    """Every complete line of a collector output file (a half-written last line is left for
    the next read). A file that does not exist yet holds no documents."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not text.endswith("\n"):
        lines.pop()
    return [json.loads(line) for line in lines if line.strip()]


@dataclass(frozen=True)
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    attributes: Attributes
    resource: Attributes


@dataclass(frozen=True)
class DataPoint:
    metric: str
    kind: str
    attributes: Attributes
    value: float
    count: int | None


def spans_in(directory: Path) -> list[Span]:
    spans: list[Span] = []
    for document in read_documents(directory / TRACES_FILE):
        for resource_spans in document.get("resourceSpans", []):
            resource = attributes_of(resource_spans.get("resource", {}).get("attributes"))
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    spans.append(
                        Span(
                            name=span["name"],
                            trace_id=span["traceId"],
                            span_id=span["spanId"],
                            parent_span_id=span.get("parentSpanId", ""),
                            attributes=attributes_of(span.get("attributes")),
                            resource=resource,
                        )
                    )
    return spans


def data_points_in(directory: Path) -> list[DataPoint]:
    points: list[DataPoint] = []
    for document in read_documents(directory / METRICS_FILE):
        for resource_metrics in document.get("resourceMetrics", []):
            for scope_metrics in resource_metrics.get("scopeMetrics", []):
                for metric in scope_metrics.get("metrics", []):
                    for kind in ("sum", "gauge", "histogram"):
                        for point in metric.get(kind, {}).get("dataPoints", []):
                            raw = point.get("asInt", point.get("asDouble", point.get("sum", 0)))
                            count = point.get("count")
                            points.append(
                                DataPoint(
                                    metric=metric["name"],
                                    kind=kind,
                                    attributes=attributes_of(point.get("attributes")),
                                    value=float(raw),
                                    count=int(count) if count is not None else None,
                                )
                            )
    return points


async def wait_until[T](read: Callable[[], T | None], *, timeout: float, what: str) -> T:
    """Poll ``read`` until it returns something other than ``None``, within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        found = read()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not reach the collector within {timeout} s")
        await asyncio.sleep(POLL_SECONDS)


def whole_output(directory: Path) -> str:
    """Everything the collector received, as text (for the 'nothing leaked' scan)."""
    return "\n".join(
        (directory / name).read_text(encoding="utf-8") if (directory / name).is_file() else ""
        for name in (TRACES_FILE, METRICS_FILE)
    )
