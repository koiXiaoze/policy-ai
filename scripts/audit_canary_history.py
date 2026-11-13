#!/usr/bin/env python3
"""Audit a completed 14-day GitHub Actions canary window.

The auditor reads GitHub Actions metadata, the redacted canary artifact, and
the public job log. It never prints source URLs, credentials, page bodies, or
request headers. A successful result is release evidence, not permission to
change repository visibility by itself.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

API_ROOT = "https://api.github.com"
DEFAULT_REPO = "koiXiaoze/policy-ai"
CANARY_WORKFLOW = ".github/workflows/source-canary.yml"
EXPECTED_SOURCE_NAMES = (
    "国务院",
    "湖北省人民政府",
    "武汉市人民政府",
    "十堰市人民政府",
    "人力资源和社会保障部",
)
WINDOW_DAYS = 14
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_API_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
PUBLIC_LOG_PATTERNS = (
    (
        "government_source_url",
        re.compile(
            r"https?://(?:[^/\s]+\.)?(?:gov\.cn|mohrss\.gov\.cn)(?:[/?\s]|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "unmasked_authorization",
        re.compile(
            r"(?:^|\s)authorization\s*[:=]\s*(?!basic\s+\*{3,})(?:\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "unmasked_cookie",
        re.compile(r"(?:^|\s)(?:set-)?cookie\s*[:=]\s*(?!\*{3,})(?:\S+)", re.IGNORECASE),
    ),
    (
        "unmasked_token",
        re.compile(r"(?:^|\s)(?:token|access_token|api[_-]?key)\s*[:=]\s*(?!\*{3,})(?:\S+)", re.IGNORECASE),
    ),
    (
        "body_field",
        re.compile(r"\b(?:item_body|content_text|request_headers)\b", re.IGNORECASE),
    ),
)


class AuditError(RuntimeError):
    """An error whose text is safe to show to an operator."""


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expose redirects so credentials are never forwarded across hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def _request(url: str, token: str | None) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "policy-ai-release-audit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=60) as response:
            data = response.read(MAX_API_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = {
            301: "GitHub API returned an unexpected redirect",
            302: "GitHub API returned an unexpected redirect",
            303: "GitHub API returned an unexpected redirect",
            307: "GitHub API returned an unexpected redirect",
            308: "GitHub API returned an unexpected redirect",
            401: "GitHub authentication failed",
            403: "GitHub API rate-limited or token lacks permission",
            404: "GitHub resource not found or token lacks access",
        }.get(exc.code, f"GitHub API returned HTTP {exc.code}")
        raise AuditError(detail) from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise AuditError(f"GitHub API request failed: {exc.__class__.__name__}") from exc
    if len(data) > MAX_API_BYTES:
        raise AuditError("GitHub API response exceeds size limit")
    return data


def _json(url: str, token: str | None) -> dict[str, Any]:
    raw = _request(url, token)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError("GitHub API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AuditError("GitHub API JSON root is not an object")
    return payload


def _paged_items(url: str, token: str | None, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        payload = _json(next_url, token)
        page = payload.get(key, [])
        if not isinstance(page, list):
            raise AuditError(f"GitHub API field {key} is not a list")
        if any(not isinstance(item, dict) for item in page):
            raise AuditError(f"GitHub API field {key} contains non-object entries")
        items.extend(page)
        link = payload.get("_links", {}) if isinstance(payload.get("_links"), dict) else {}
        next_link = link.get("next", {}) if isinstance(link.get("next"), dict) else {}
        next_url = next_link.get("url") if isinstance(next_link.get("url"), str) else None
    return items


def _date_from_iso(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING).date()


def _beijing_window_end(today: date | None = None) -> date:
    """Return yesterday in Beijing; a same-day schedule is not yet auditable."""
    current = today or datetime.now(timezone.utc).astimezone(BEIJING).date()
    return current - timedelta(days=1)


def _successful_runs_by_date(runs: Iterable[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    """Group successful scheduled runs by Beijing date, newest first."""
    result: dict[date, list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("event") != "schedule" or run.get("status") != "completed":
            continue
        if run.get("conclusion") != "success":
            continue
        run_date = _date_from_iso(run.get("created_at"))
        if run_date is None:
            continue
        result.setdefault(run_date, []).append(run)
    for candidates in result.values():
        candidates.sort(key=lambda run: str(run.get("created_at", "")), reverse=True)
    return result


def _github_redirect(url: str, token: str | None) -> str:
    """Resolve a GitHub download redirect to its temporary signed URL."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "policy-ai-release-audit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        _NO_REDIRECT_OPENER.open(request, timeout=60).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise AuditError(f"download redirect returned HTTP {exc.code}") from exc
        location = exc.headers.get("Location")
        if not isinstance(location, str) or not location:
            raise AuditError("download redirect has no Location")
        resolved = urllib.parse.urljoin(url, location)
        if urllib.parse.urlsplit(resolved).scheme not in ("http", "https"):
            raise AuditError("download redirect uses an unsupported scheme")
        return resolved
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise AuditError(f"download redirect failed: {exc.__class__.__name__}") from exc
    raise AuditError("download endpoint did not return a redirect")


def _download_bounded(url: str, *, auth_token: str | None, limit: int, category: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise AuditError(f"{category} URL uses an unsupported scheme")
    headers = {"User-Agent": "policy-ai-release-audit"}
    if auth_token:
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {auth_token}",
            }
        )
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise AuditError(f"{category} download returned HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise AuditError(f"{category} download failed: {exc.__class__.__name__}") from exc
    if len(data) > limit:
        raise AuditError(f"{category} exceeds size limit")
    return data


def _download_artifact(archive_url: str, token: str | None) -> bytes:
    signed_url = _github_redirect(archive_url, token)
    return _download_bounded(
        signed_url,
        auth_token=None,
        limit=MAX_ARTIFACT_BYTES,
        category="artifact",
    )


def _read_manifest_from_zip(data: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if names != ["release-manifest.json"]:
                raise AuditError("artifact must contain exactly release-manifest.json")
            info = archive.getinfo("release-manifest.json")
            if info.file_size < 0 or info.file_size > MAX_MANIFEST_BYTES:
                raise AuditError("release manifest exceeds size limit")
            raw = archive.read(info)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise AuditError("artifact is not a readable canary ZIP") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError("release manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise AuditError("release manifest root is not an object")
    return payload


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    run: dict[str, Any],
    day: date,
) -> list[str]:
    errors: list[str] = []
    if manifest.get("status") != "ok":
        errors.append("manifest status is not ok")
    if manifest.get("event") != "schedule":
        errors.append("manifest event is not schedule")
    if manifest.get("beijing_date") != day.isoformat():
        errors.append("manifest date does not match run date")
    run_id = run.get("id")
    if not _is_positive_int(run_id) or manifest.get("run_id") != str(run_id):
        errors.append("manifest run_id does not match run")
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        errors.append("run head SHA is invalid")
    elif manifest.get("commit_sha") != head_sha:
        errors.append("manifest commit does not match run head")
    expected = list(EXPECTED_SOURCE_NAMES)
    window_days = manifest.get("window_days")
    if not _is_positive_int(window_days) or window_days != WINDOW_DAYS:
        errors.append("manifest window_days is not 14")
    if manifest.get("expected_source_names") != expected:
        errors.append("manifest expected source list differs")
    source_count = manifest.get("source_count")
    if not _is_positive_int(source_count) or source_count != len(expected):
        errors.append("manifest source_count is not 5")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        errors.append("manifest sources is not a list")
        return errors
    names: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            errors.append("manifest source entry is not an object")
            continue
        name = source.get("source_name")
        names.append(name if isinstance(name, str) else "<invalid>")
        if source.get("status") != "ok":
            errors.append("source status is not ok")
        if source.get("fetch_status") != "ok":
            errors.append("source fetch status is not ok")
        if not _is_positive_int(source.get("item_count")):
            errors.append("source item count is not positive")
        if source.get("content_status") != "ok":
            errors.append("source content status is not ok")
        content_chars = source.get("content_chars")
        if not _is_positive_int(content_chars) or content_chars < 100:
            errors.append("source content length is below threshold")
    if len(sources) != len(expected):
        errors.append("manifest source count does not match fixed five")
    if len(names) != len(set(names)):
        errors.append("manifest source names are duplicated")
    if names != expected:
        errors.append("manifest source names/order differ from fixed five")
    if manifest.get("validation_errors"):
        errors.append("manifest contains validation errors")
    return errors


def _read_public_log(job_id: int, token: str | None, repo: str) -> bytes:
    api_url = f"{API_ROOT}/repos/{repo}/actions/jobs/{job_id}/logs"
    signed_url = _github_redirect(api_url, token)
    return _download_bounded(
        signed_url,
        auth_token=None,
        limit=MAX_LOG_BYTES,
        category="public job log",
    )


def _scan_public_log(data: bytes) -> list[str]:
    if len(data) > MAX_LOG_BYTES:
        return ["public job log exceeds size limit"]
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return ["public job log could not be decoded"]
    hits: list[str] = []
    for category, pattern in PUBLIC_LOG_PATTERNS:
        if pattern.search(text):
            hits.append(f"public job log contains {category}")
    return hits


def _artifact_for_run(run_id: int, token: str | None, repo: str) -> dict[str, Any] | None:
    payload = _json(f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", token)
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return None
    artifact = artifacts[0]
    if not isinstance(artifact, dict):
        return None
    if not _is_positive_int(artifact.get("id")):
        return None
    if artifact.get("name") != f"government-source-canary-{run_id}" or artifact.get("expired") is not False:
        return None
    if not isinstance(artifact.get("archive_download_url"), str):
        return None
    return artifact


def _audit_day(
    day: date,
    runs: Iterable[dict[str, Any]],
    *,
    token: str | None,
    repo: str,
) -> tuple[dict[str, Any], list[str]]:
    candidates = list(runs)
    if not candidates:
        errors = ["no successful event=schedule run"]
        return {
            "date": day.isoformat(),
            "run_id": None,
            "artifact_id": None,
            "sources": [],
            "status": "failed",
            "errors": errors,
        }, errors

    attempts: list[str] = []
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "run_id": None,
        "artifact_id": None,
        "sources": [],
        "status": "failed",
        "errors": [],
    }
    for run in candidates:
        candidate_result, errors = _audit_run(day, run, token=token, repo=repo)
        if not errors:
            return candidate_result, []
        result = candidate_result
        run_id = run.get("id")
        shown_run_id = str(run_id) if _is_positive_int(run_id) else "<invalid>"
        attempts.append(f"run {shown_run_id}: {'; '.join(errors)}")

    result["errors"] = attempts
    return result, attempts


def _audit_run(
    day: date,
    run: dict[str, Any],
    *,
    token: str | None,
    repo: str,
) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "run_id": run.get("id"),
        "artifact_id": None,
        "sources": [],
        "status": "failed",
        "errors": [],
    }
    errors: list[str] = []
    run_id = run.get("id")
    if not _is_positive_int(run_id):
        errors.append("run id is invalid")
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None:
        errors.append("run head SHA is invalid")
    if _date_from_iso(run.get("created_at")) != day:
        errors.append("run creation date does not match Beijing date")
    if errors:
        result["errors"] = errors
        return result, errors
    run_id = int(run_id)

    jobs = _paged_items(
        f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
        token,
        "jobs",
    )
    jobs = [job for job in jobs if job.get("name") == "Verify all enabled government sources"]
    if len(jobs) != 1:
        errors.append("expected exactly one canary job")
    elif jobs[0].get("conclusion") != "success":
        errors.append("canary job did not succeed")
    else:
        try:
            job_id = jobs[0].get("id")
            if not _is_positive_int(job_id):
                raise AuditError("public job id is invalid")
            log = _read_public_log(int(job_id), token, repo)
            errors.extend(_scan_public_log(log))
        except (AuditError, KeyError, ValueError, TypeError) as exc:
            if isinstance(exc, AuditError):
                errors.append(str(exc))
            else:
                errors.append("public job log metadata is invalid")

    artifact = _artifact_for_run(run_id, token, repo)
    if artifact is None:
        errors.append("expected exactly one unexpired canary artifact")
    else:
        result["artifact_id"] = artifact.get("id")
        try:
            archive_url = artifact.get("archive_download_url")
            if not isinstance(archive_url, str):
                raise AuditError("artifact archive URL is missing")
            manifest = _read_manifest_from_zip(_download_artifact(archive_url, token))
            errors.extend(_validate_manifest(manifest, run=run, day=day))
            sources = manifest.get("sources", [])
            if isinstance(sources, list):
                result["sources"] = [
                    {
                        "source_name": s.get("source_name"),
                        "item_count": s.get("item_count"),
                        "content_chars": s.get("content_chars"),
                    }
                    for s in sources
                    if isinstance(s, dict)
                ]
        except AuditError as exc:
            errors.append(str(exc))

    if errors:
        # Keep only safe category messages in the report. Never include manifest
        # bodies, source URLs, credentials, or raw API responses.
        result["errors"] = errors
        return result, errors
    result["status"] = "passed"
    return result, []


def _workflow_id(repo: str, token: str | None) -> int:
    workflows = _paged_items(f"{API_ROOT}/repos/{repo}/actions/workflows?per_page=100", token, "workflows")
    matches = [workflow for workflow in workflows if workflow.get("path") == CANARY_WORKFLOW]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
        raise AuditError("cannot identify source canary workflow")
    return int(matches[0]["id"])


def _fetch_runs(repo: str, token: str | None) -> list[dict[str, Any]]:
    workflow_id = _workflow_id(repo, token)
    return _paged_items(
        f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow_id}/runs?per_page=100",
        token,
        "workflow_runs",
    )


def _window_days(end: date) -> list[date]:
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return [start + timedelta(days=offset) for offset in range(WINDOW_DAYS)]


def _earliest_release_date(days: list[dict[str, Any]], end: date) -> str:
    """Return the end date when complete, else end plus missing trailing days."""
    trailing = 0
    for item in reversed(days):
        if item.get("status") != "passed":
            break
        trailing += 1
    return (end + timedelta(days=WINDOW_DAYS - trailing)).isoformat()


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO))
    parser.add_argument("--end", type=date.fromisoformat, default=_beijing_window_end())
    parser.add_argument("--output", type=Path, default=Path("reports/canary-history-audit.json"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    try:
        runs = _fetch_runs(args.repo, token)
        successful = _successful_runs_by_date(runs)
        days: list[dict[str, Any]] = []
        for day in _window_days(args.end):
            item, _ = _audit_day(day, successful.get(day, []), token=token, repo=args.repo)
            days.append(item)
        complete = all(item["status"] == "passed" for item in days)
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repository": args.repo,
            "window_end": args.end.isoformat(),
            "window_days": WINDOW_DAYS,
            "complete": complete,
            "passed_days": sum(item["status"] == "passed" for item in days),
            "days": days,
            "earliest_release_date": _earliest_release_date(days, args.end),
        }
        _write_report(args.output, report)
        for item in days:
            print(
                json.dumps(
                    {
                        "date": item["date"],
                        "status": item["status"],
                        "run_id": item["run_id"],
                        "artifact_id": item["artifact_id"],
                        "errors": item["errors"],
                    },
                    ensure_ascii=False,
                )
            )
        print(
            json.dumps(
                {
                    "complete": complete,
                    "passed_days": report["passed_days"],
                    "required_days": WINDOW_DAYS,
                    "earliest_release_date": report["earliest_release_date"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if complete else 1
    except AuditError as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 2
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"Audit failed: malformed metadata or report path ({exc.__class__.__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
