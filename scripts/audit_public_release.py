#!/usr/bin/env python3
"""Audit a Git revision before a repository is made public.

Only safe categories and repository paths are printed. Secret values and
matched content are never emitted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
from pathlib import Path


SECRET_PATTERNS = {
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "openai_style_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "google_api_key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "credential_url": re.compile(rb"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    "wecom_webhook": re.compile(
        rb"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{20,}",
        re.IGNORECASE,
    ),
    "feishu_webhook": re.compile(
        rb"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]{20,}",
        re.IGNORECASE,
    ),
}

FORBIDDEN_PATH_PARTS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
    "logs",
    "reports",
    "dist",
    "build",
    ".cache",
    ".tmp",
    "tests",
}
SENSITIVE_ENV_KEY = re.compile(
    r"(?:^|_)(?:API_KEY|APP_SECRET|SECRET|TOKEN|PASSWORD|SMTP_PASSWORD|"
    r"WEBHOOK_URL|DATABASE_URL|SMTP_USER|NOTIFY_EMAIL|APP_ID|CORP_ID|BOT_ID|"
    r"ALLOWED_USER_IDS|ENCODING_AES_KEY|AGENT_ID)(?:$|_)",
    re.IGNORECASE,
)
PLACEHOLDER_MARKERS = (
    "your-",
    "example",
    "replace",
    "changeme",
    "<",
    ">",
    "placeholder",
)


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _history_objects(revision: str) -> list[tuple[str, str]]:
    output = _git("rev-list", "--objects", revision).decode("utf-8", errors="replace")
    objects: list[tuple[str, str]] = []
    for line in output.splitlines():
        object_sha, _, path = line.partition(" ")
        objects.append((object_sha, path))
    return objects


def _current_paths(revision: str) -> list[str]:
    output = _git("ls-tree", "-r", "-z", "--name-only", revision)
    return [item for item in output.decode("utf-8", errors="replace").split("\0") if item]


def _sensitive_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'\"")
        if not SENSITIVE_ENV_KEY.search(key) or len(value) < 8:
            continue
        lowered = value.lower()
        if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
            continue
        if key.upper() == "DATABASE_URL" and value.startswith("sqlite:"):
            continue
        values[key] = value
    return values


def _is_forbidden_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    name = parts[-1]
    if ".env" in parts:
        return True
    if any(part in FORBIDDEN_PATH_PARTS for part in parts):
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if re.search(r"\.(?:db|sqlite|db-wal|db-shm|sqlite-wal|sqlite-shm|log|bak)$", name, re.IGNORECASE):
        return True
    return False


def _remote_refs() -> list[str]:
    output = _git(
        "for-each-ref",
        "--format=%(refname)",
        "refs/remotes/origin",
    ).decode("utf-8", errors="replace")
    refs = []
    for ref in output.splitlines():
        if not ref or ref == "refs/remotes/origin/HEAD":
            continue
        refs.append(ref)
    if not refs:
        raise RuntimeError("no origin remote refs found")
    return sorted(refs)


def _audit_revision(revision: str, env_file: Path) -> bool:
    commit_sha = _git("rev-parse", revision).decode().strip()
    current_paths = _current_paths(revision)
    objects = _history_objects(revision)
    historical_paths = {path for _, path in objects if path}
    forbidden_paths = sorted(
        path for path in current_paths + sorted(historical_paths) if _is_forbidden_path(path)
    )
    forbidden_paths = sorted(set(forbidden_paths))

    blob_hits: set[tuple[str, str]] = set()
    blobs_scanned = 0
    for object_sha, path in objects:
        object_type = _git("cat-file", "-t", object_sha).decode().strip()
        if object_type != "blob":
            continue
        blobs_scanned += 1
        content = _git("cat-file", "blob", object_sha)
        for category, pattern in SECRET_PATTERNS.items():
            if not pattern.search(content):
                continue
            if category == "credential_url":
                # Documented placeholders must not mask a real credential in
                # the same blob, so scrub only the known examples and recheck.
                scrubbed = content.replace(b"user:password@127.0.0.1", b"")
                scrubbed = scrubbed.replace(b"driver://user:pass@localhost/dbname", b"")
                if not pattern.search(scrubbed):
                    continue
            blob_hits.add((path or object_sha, category))
    env_values = _sensitive_env_values(env_file)
    env_hits: set[str] = set()
    if env_values:
        content_blobs: list[tuple[str, bytes]] = []
        for object_sha, path in objects:
            if _git("cat-file", "-t", object_sha).decode().strip() != "blob":
                continue
            content_blobs.append((path or object_sha, _git("cat-file", "blob", object_sha)))
        for key, value in env_values.items():
            needle = value.encode("utf-8")
            if any(needle in content for _, content in content_blobs):
                env_hits.add(key)

    commit_messages = _git("log", "--format=%B", revision)
    commit_hits = {
        category
        for category, pattern in SECRET_PATTERNS.items()
        if pattern.search(commit_messages)
    }

    failures = bool(forbidden_paths or blob_hits or env_hits or commit_hits)
    print(f"Audit revision: {revision} ({commit_sha})")
    print(f"Current tracked files: {len(current_paths)}")
    print(f"Git blobs scanned: {blobs_scanned}")
    print(f"Configured sensitive values checked: {len(env_values)}")
    print(f"Forbidden current or historical paths: {len(forbidden_paths)}")
    print(f"Sensitive pattern findings: {len(blob_hits)}")
    print(f"Configured value hits in history: {len(env_hits)}")
    print(f"Sensitive patterns in commit messages: {len(commit_hits)}")
    for path in forbidden_paths:
        print(f"FORBIDDEN_PATH {path}")
    for path, category in sorted(blob_hits):
        print(f"SECRET_PATTERN {category} {path}")
    for key in sorted(env_hits):
        print(f"ENV_VALUE_HIT {key}")
    for category in sorted(commit_hits):
        print(f"COMMIT_MESSAGE_HIT {category}")
    print("Result: FAIL" if failures else "Result: PASS")
    return not failures


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default="HEAD")
    parser.add_argument("--all-remote-refs", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    if args.all_remote_refs and args.rev != "HEAD":
        parser.error("--rev cannot be combined with --all-remote-refs")

    try:
        revisions = _remote_refs() if args.all_remote_refs else [args.rev]
        results = [(revision, _audit_revision(revision, args.env_file)) for revision in revisions]
    except (RuntimeError, subprocess.CalledProcessError, UnicodeError, OSError) as exc:
        print(f"Audit setup failed: {exc.__class__.__name__}", file=sys.stderr)
        return 2

    passed = sum(result for _, result in results)
    overall_passed = passed == len(results)
    if args.json_output:
        try:
            _write_report(
                args.json_output,
                {
                    "schema_version": 1,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "all-origin-refs" if args.all_remote_refs else "revision",
                    "ref_count": len(results),
                    "passed_count": passed,
                    "overall_result": "pass" if overall_passed else "fail",
                    "revisions": [
                        {"ref": revision, "result": "pass" if result else "fail"}
                        for revision, result in results
                    ],
                },
            )
        except OSError as exc:
            print(f"Audit report write failed: {exc.__class__.__name__}", file=sys.stderr)
            return 2

    print(f"Remote refs audited: {len(results)}")
    print(f"Remote refs passed: {passed}")
    print("Overall result: PASS" if overall_passed else "Overall result: FAIL")
    return 0 if overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
