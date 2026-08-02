#!/usr/bin/env python3
"""Validate staged note changes before an autonomous commit."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


BLOCKED_ROOTS = {"attachments", "assets", "images"}
SKIPPED_SEARCH_ROOTS = {".git", ".voice-memo-automation"}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def marker_count(repo: Path, marker: str) -> int:
    count = 0
    for path in repo.rglob("*.md"):
        relative = path.relative_to(repo)
        if any(part in SKIPPED_SEARCH_ROOTS for part in relative.parts):
            continue
        count += path.read_text(encoding="utf-8", errors="replace").count(marker)
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--memo-id", type=int, required=True)
    parser.add_argument("--max-files", type=int, default=5)
    args = parser.parse_args()
    repo = args.repo.resolve()
    errors: list[str] = []

    status = git(repo, "diff", "--cached", "--name-status").stdout.splitlines()
    if not status:
        errors.append("no staged changes")

    paths: list[str] = []
    for line in status:
        fields = line.split("\t")
        change = fields[0]
        if change not in {"A", "M"}:
            errors.append(f"unsupported change {change}: {' '.join(fields[1:])}")
            continue
        path = fields[-1]
        paths.append(path)
        candidate = Path(path)
        if candidate.suffix.lower() != ".md":
            errors.append(f"non-Markdown change: {path}")
        if any(part.startswith(".") for part in candidate.parts):
            errors.append(f"dot-directory change: {path}")
        if candidate.parts and candidate.parts[0] in BLOCKED_ROOTS:
            errors.append(f"blocked content directory: {path}")
        if "transcript" in candidate.name.lower():
            errors.append(f"transcript-like filename: {path}")

    if len(paths) > args.max_files:
        errors.append(f"too many files: {len(paths)} > {args.max_files}")

    for line in git(repo, "diff", "--cached", "--numstat").stdout.splitlines():
        added, deleted, path = line.split("\t", 2)
        if deleted != "0":
            errors.append(f"deleted lines are not allowed: {path} ({deleted})")

    diff_check = git(repo, "diff", "--cached", "--check", check=False)
    if diff_check.returncode:
        errors.append(diff_check.stdout.strip() or diff_check.stderr.strip())

    marker = f"<!-- voice-memo-id:{args.memo_id} -->"
    count = marker_count(repo, marker)
    if count != 1:
        errors.append(f"expected one provenance marker, found {count}")

    result = {"ok": not errors, "files": paths, "marker": marker, "errors": errors}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
