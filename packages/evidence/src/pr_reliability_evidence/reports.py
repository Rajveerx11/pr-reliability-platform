"""Bounded JUnit summaries. Report text and testcase names are never retained."""

import math
from xml.parsers import expat

MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_FILES = 4
MAX_XML_ELEMENTS = 20_000


class ReportError(ValueError):
    """The untrusted report cannot be safely summarized."""


def summarize_junit(raw: bytes) -> dict[str, int]:
    if not raw or len(raw) > MAX_REPORT_BYTES:
        raise ReportError("report_invalid")
    parser = expat.ParserCreate()
    summary = {"passed": 0, "failed": 0, "skipped": 0, "duration_ms": 0}
    depth = elements = 0
    active = None
    root = None

    def reject(*args):
        raise ReportError("report_invalid")

    def start(name, attrs):
        nonlocal depth, elements, active, root
        depth += 1
        elements += 1
        if depth > 32 or elements > MAX_XML_ELEMENTS:
            reject()
        if root is None:
            root = name
            if name not in {"testsuite", "testsuites"}:
                reject()
        if name == "testcase":
            if active is not None:
                reject()
            try:
                seconds = float(attrs.get("time", "0"))
                if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
                    reject()
            except ValueError:
                reject()
            active = {"status": "passed", "duration_ms": round(seconds * 1000)}
        elif active is not None:
            if name in {"failure", "error"}:
                active["status"] = "failed"
            elif name == "skipped" and active["status"] != "failed":
                active["status"] = "skipped"

    def end(name):
        nonlocal depth, active
        if name == "testcase":
            if active is None:
                reject()
            summary[active["status"]] += 1
            summary["duration_ms"] += active["duration_ms"]
            active = None
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(raw, True)
    except (expat.ExpatError, ValueError, OverflowError) as exc:
        raise ReportError("report_invalid") from exc
    return summary
