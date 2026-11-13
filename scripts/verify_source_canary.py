#!/usr/bin/env python3
"""Validate a government source canary result and emit release evidence.

The manifest intentionally excludes page bodies, source URLs, request headers,
cookies, and credentials. It records only the fixed source-name gate, aggregate
counts, execution metadata, and per-source verification counters.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.source_canary_diagnostics import (  # noqa: E402
    ALLOWED_CHANNEL_FAILURES,
    ALLOWED_ERROR_TYPES,
    ALLOWED_FAILURE_CATEGORIES,
)


EXPECTED_SOURCE_NAMES = (
    "国务院",
    "湖北省人民政府",
    "武汉市人民政府",
    "十堰市人民政府",
    "人力资源和社会保障部",
)
MIN_CONTENT_CHARS = 100
SCHEMA_VERSION = 1
WINDOW_DAYS = 14
MAX_LOG_BYTES = 256 * 1024
LOG_SENSITIVE_PATTERNS = (
    ("url", re.compile(r"https?://|\bwww\.", re.IGNORECASE)),
    ("authorization", re.compile(r"\bauthorization\b", re.IGNORECASE)),
    ("cookie", re.compile(r"\b(?:set-)?cookie\b", re.IGNORECASE)),
    ("token", re.compile(r"token", re.IGNORECASE)),
    ("body", re.compile(r"\b(?:item_body|content_text|request_headers)\b", re.IGNORECASE)),
)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.exists():
        return None, [f"canary report not found: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [f"canary report is not valid JSON: {exc.__class__.__name__}"]
    if not isinstance(payload, dict):
        return None, ["canary report root must be an object"]
    return payload, []


def _sanitize_diagnostics(
    name: str,
    field: str,
    raw_values: object,
    allowed: frozenset[str],
    errors: list[str],
) -> list[str]:
    """Return only fixed diagnostic enums, without echoing rejected values."""
    if raw_values is None:
        return []
    if not isinstance(raw_values, list):
        errors.append(f"{name}: {field} must be a list")
        return []

    safe: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_values):
        if not isinstance(value, str):
            errors.append(f"{name}: {field} contains non-string value at index {index}")
            continue
        if value not in allowed:
            errors.append(f"{name}: {field} contains invalid value at index {index}")
            continue
        if value in seen:
            errors.append(f"{name}: {field} contains duplicate value at index {index}")
            continue
        seen.add(value)
        safe.append(value)
    return safe


def _sanitize_enum(
    name: str,
    field: str,
    raw_value: object,
    allowed: frozenset[str],
    errors: list[str],
) -> str | None:
    """Accept only a fixed enum, without echoing arbitrary input."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or raw_value not in allowed:
        errors.append(f"{name}: {field} is invalid")
        return None
    return raw_value


def _sanitize_error_type(
    name: str,
    raw_value: object,
    errors: list[str],
) -> str | None:
    """Accept only a known error enum or a missing value."""
    return _sanitize_enum(
        name,
        "error_type",
        raw_value,
        ALLOWED_ERROR_TYPES,
        errors,
    )


def _sanitize_optional_nonnegative_int(
    name: str,
    field: str,
    raw_value: object,
    errors: list[str],
) -> int | None:
    """Accept a missing value or a non-negative integer; bool is invalid."""
    if raw_value is None:
        return None
    if isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
        return raw_value
    errors.append(f"{name}: {field} must be a non-negative integer or null")
    return None


def _validate(payload: dict[str, Any] | None) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if payload is None:
        return errors, []

    if payload.get("status") != "ok":
        errors.append("canary status is not ok")
    if payload.get("check_content") is not True:
        errors.append("canary did not enable content verification")

    threshold = payload.get("min_content_chars")
    if not isinstance(threshold, int) or threshold < MIN_CONTENT_CHARS:
        errors.append(f"content threshold must be at least {MIN_CONTENT_CHARS}")
        threshold = MIN_CONTENT_CHARS

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append("canary summary is missing or invalid")
        summary = {}
    if summary.get("total") != len(EXPECTED_SOURCE_NAMES):
        errors.append(f"summary.total must equal {len(EXPECTED_SOURCE_NAMES)}")
    if summary.get("ok") != len(EXPECTED_SOURCE_NAMES):
        errors.append(f"summary.ok must equal {len(EXPECTED_SOURCE_NAMES)}")
    if summary.get("failed") != 0:
        errors.append("summary.failed must equal 0")

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        errors.append("canary sources must be a list")
        raw_sources = []

    safe_sources: list[dict[str, Any]] = []
    observed_names: list[str] = []
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            errors.append(f"source entry {index} must be an object")
            continue
        name = raw_source.get("source_name")
        if isinstance(name, str) and name in EXPECTED_SOURCE_NAMES:
            name_text = name
            observed_names.append(name_text)
        else:
            name_text = f"<invalid-source-{index}>"
            errors.append(f"source entry {index}: source_name is invalid")
        failed_channels = _sanitize_diagnostics(
            name_text,
            "failed_channels",
            raw_source.get("failed_channels"),
            ALLOWED_CHANNEL_FAILURES,
            errors,
        )
        failure_categories = _sanitize_diagnostics(
            name_text,
            "failure_categories",
            raw_source.get("failure_categories"),
            ALLOWED_FAILURE_CATEGORIES,
            errors,
        )
        error_type = _sanitize_error_type(
            name_text,
            raw_source.get("error_type"),
            errors,
        )
        entry = {
            "source_name": name_text,
            "status": _sanitize_enum(
                name_text,
                "status",
                raw_source.get("status"),
                frozenset({"ok", "failed"}),
                errors,
            ),
            "fetch_status": _sanitize_enum(
                name_text,
                "fetch_status",
                raw_source.get("fetch_status"),
                frozenset({"ok", "empty", "degraded", "failed", "unknown"}),
                errors,
            ),
            "item_count": _sanitize_optional_nonnegative_int(
                name_text,
                "item_count",
                raw_source.get("item_count", None),
                errors,
            ),
            "content_status": _sanitize_enum(
                name_text,
                "content_status",
                raw_source.get("content_status"),
                frozenset(
                    {"ok", "too_short", "empty", "failed", "skipped", "not_checked"}
                ),
                errors,
            ),
            "content_chars": _sanitize_optional_nonnegative_int(
                name_text,
                "content_chars",
                raw_source.get("content_chars", None),
                errors,
            ),
            "error_type": error_type,
            "failed_channels": failed_channels,
            "failure_categories": failure_categories,
        }
        safe_sources.append(entry)

        if entry["status"] != "ok":
            errors.append(f"{name_text}: status is not ok")
        if entry["fetch_status"] != "ok":
            errors.append(f"{name_text}: fetch_status is not ok")
        if not isinstance(entry["item_count"], int) or entry["item_count"] <= 0:
            errors.append(f"{name_text}: item_count must be positive")
        if entry["content_status"] != "ok":
            errors.append(f"{name_text}: content_status is not ok")
        if not isinstance(entry["content_chars"], int) or entry["content_chars"] < threshold:
            errors.append(f"{name_text}: content_chars is below threshold")

    if len(observed_names) != len(set(observed_names)):
        errors.append("canary contains duplicate source names")
    if set(observed_names) != set(EXPECTED_SOURCE_NAMES):
        missing = sorted(set(EXPECTED_SOURCE_NAMES) - set(observed_names))
        if missing:
            errors.append(f"missing fixed sources: {', '.join(missing)}")
    if len(raw_sources) != len(EXPECTED_SOURCE_NAMES):
        errors.append(f"source_count must equal {len(EXPECTED_SOURCE_NAMES)}")

    return errors, safe_sources


def _beijing_date(payload: dict[str, Any] | None) -> str:
    checked_at = payload.get("checked_at") if payload else None
    if isinstance(checked_at, str):
        try:
            value = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _validate_logs(paths: list[Path]) -> list[str]:
    """Scan canary logs without echoing matched values or surrounding text."""
    errors: list[str] = []
    for path in paths:
        if not path.exists():
            errors.append(f"canary log is missing: {path.name}")
            continue
        try:
            size = path.stat().st_size
            if size > MAX_LOG_BYTES:
                errors.append(f"canary log exceeds {MAX_LOG_BYTES} bytes: {path.name}")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"cannot read canary log {path.name}: {exc.__class__.__name__}")
            continue
        for category, pattern in LOG_SENSITIVE_PATTERNS:
            if pattern.search(text):
                errors.append(f"canary log contains forbidden {category} pattern: {path.name}")
    return errors


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "## Government Source Canary",
        "",
        f"- Gate: **{manifest['status'].upper()}**",
        f"- Beijing date: `{manifest['beijing_date']}`",
        f"- Event: `{manifest.get('event') or 'local'}`",
        f"- Run: `{manifest.get('run_id') or 'local'}`",
        "",
        "| Source | Status | Items | Content chars | Failed channels | Failure categories |",
        "|---|---|---:|---:|---|---|",
    ]
    for source in manifest["sources"]:
        failed_channels = ", ".join(source.get("failed_channels", [])) or "-"
        failure_categories = ", ".join(source.get("failure_categories", [])) or "-"
        lines.append(
            f"| {source['source_name']} | {source['status']} | "
            f"{source['item_count']} | {source['content_chars']} | "
            f"{failed_channels} | {failure_categories} |"
        )
    if manifest["validation_errors"]:
        lines.extend(["", "Validation errors:", ""])
        lines.extend(f"- {item}" for item in manifest["validation_errors"])
    return "\n".join(lines) + "\n"

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        type=Path,
        help="Canary/diagnostic log that must not contain URLs or sensitive fields",
    )
    args = parser.parse_args()

    payload, load_errors = _read_json(args.input)
    validation_errors, sources = _validate(payload)
    log_errors = _validate_logs(args.log)
    errors = load_errors + validation_errors + log_errors
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "beijing_date": _beijing_date(payload),
        "status": "ok" if not errors else "failed",
        "event": __import__("os").environ.get("GITHUB_EVENT_NAME", "local"),
        "workflow": __import__("os").environ.get("GITHUB_WORKFLOW", "local"),
        "run_id": __import__("os").environ.get("GITHUB_RUN_ID"),
        "run_attempt": __import__("os").environ.get("GITHUB_RUN_ATTEMPT"),
        "commit_sha": __import__("os").environ.get("GITHUB_SHA"),
        "ref_name": __import__("os").environ.get("GITHUB_REF_NAME", "local"),
        "expected_source_names": list(EXPECTED_SOURCE_NAMES),
        "source_count": len(sources),
        "window_days": WINDOW_DAYS,
        "sources": sources,
        "validation_errors": errors,
    }
    _write_json(args.manifest_output, manifest)

    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_output.open("a", encoding="utf-8") as handle:
            handle.write(_markdown(manifest))

    print(
        json.dumps(
            {
                "status": manifest["status"],
                "beijing_date": manifest["beijing_date"],
                "event": manifest["event"],
                "source_count": manifest["source_count"],
                "validation_error_count": len(errors),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
