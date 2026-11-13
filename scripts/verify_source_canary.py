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
import sys
from typing import Any
from zoneinfo import ZoneInfo


EXPECTED_SOURCE_NAMES = (
    "国务院",
    "湖北省人民政府",
    "武汉市人民政府",
    "十堰市人民政府",
    "人力资源和社会保障部",
)
MIN_CONTENT_CHARS = 100
SCHEMA_VERSION = 1


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
        name_text = name if isinstance(name, str) else f"<invalid-{index}>"
        observed_names.append(name_text)
        entry = {
            "source_name": name_text,
            "status": raw_source.get("status"),
            "fetch_status": raw_source.get("fetch_status"),
            "item_count": raw_source.get("item_count"),
            "content_status": raw_source.get("content_status"),
            "content_chars": raw_source.get("content_chars"),
            "error_type": raw_source.get("error_type"),
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
        unexpected = sorted(set(observed_names) - set(EXPECTED_SOURCE_NAMES))
        if missing:
            errors.append(f"missing fixed sources: {', '.join(missing)}")
        if unexpected:
            errors.append(f"unexpected sources: {', '.join(unexpected)}")
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
        "| Source | Status | Items | Content chars |",
        "|---|---|---:|---:|",
    ]
    for source in manifest["sources"]:
        lines.append(
            f"| {source['source_name']} | {source['status']} | "
            f"{source['item_count']} | {source['content_chars']} |"
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
    args = parser.parse_args()

    payload, load_errors = _read_json(args.input)
    validation_errors, sources = _validate(payload)
    errors = load_errors + validation_errors
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
