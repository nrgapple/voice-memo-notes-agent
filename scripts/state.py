#!/usr/bin/env python3
"""Atomic state management for the Voice Memos notes workflow."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def git_value(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def repository_slug(repo: Path) -> str:
    remote = git_value(repo, "remote", "get-url", "origin")
    if remote.startswith("git@github.com:"):
        return remote.removeprefix("git@github.com:").removesuffix(".git")
    if remote.startswith("https://github.com/"):
        return remote.removeprefix("https://github.com/").removesuffix(".git")
    return ""


def current_branch(repo: Path) -> str:
    return git_value(repo, "branch", "--show-current") or "main"


class Store:
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.root = self.repo / ".voice-memo-automation"
        self.state_path = self.root / "state.json"
        self.config_path = self.root / "config.json"
        self.lock_path = self.root / "state.lock"
        self.transcripts = self.root / "transcripts"
        self.failures = self.root / "failures.jsonl"

    def setup(self) -> None:
        self.transcripts.mkdir(parents=True, exist_ok=True)
        config_exists = self.config_path.exists()
        defaults = {
            "repository": os.environ.get("VOICE_MEMO_NOTES_REPOSITORY") or repository_slug(self.repo),
            "branch": os.environ.get("VOICE_MEMO_NOTES_BRANCH") or current_branch(self.repo),
            "language": "en-US",
            "max_memos_per_run": 5,
            "max_files_per_memo": 5,
            "transcript_retention_days": 30,
            "semantic_model": os.environ.get("VOICE_MEMO_SEMANTIC_MODEL", ""),
            "semantic_reasoning_effort": "medium",
            "publish_mode": "review",
            "review_branch_prefix": "voice-memo/review",
            "command_timeout_seconds": 120,
            "semantic_timeout_seconds": 180,
            "transcript_max_characters": 50000,
            "candidate_file_limit": 5,
            "candidate_excerpt_characters": 6000,
            "candidate_total_characters": 30000,
            "vault_map_max_files": 1000,
            "vault_map_total_characters": 50000,
            "candidate_graph_total_characters": 12000,
            "semantic_prompt_max_characters": 120000,
            "readiness_timeout_seconds": 20,
            "readiness_stable_checks": 2,
            "required_trigger_phrases": [
                "work note",
                "work notes",
                "for work",
                "work memo",
                "memo for work",
                "note for work",
            ],
        }
        config = json.loads(self.config_path.read_text(encoding="utf-8")) if config_exists else {}
        original_config = dict(config)
        if config_exists and int(config.get("configuration_version", 1)) < 3:
            config.update({
                "semantic_reasoning_effort": "medium",
                "candidate_excerpt_characters": 6000,
                "candidate_total_characters": 30000,
                "configuration_version": 3,
            })
        if config_exists and int(config.get("configuration_version", 1)) < 4:
            config.setdefault("publish_mode", "direct")
        config["configuration_version"] = 4
        for key, value in defaults.items():
            config.setdefault(key, value)
        if config != original_config or not config_exists:
            self._atomic_write(self.config_path, config)
        if not self.state_path.exists():
            self._atomic_write(
                self.state_path,
                {
                    "version": 1,
                    "baseline_complete": False,
                    "initialized_at": None,
                    "lease": None,
                    "records": {},
                },
            )
        self.lock_path.touch(exist_ok=True)

    @contextmanager
    def locked(self):
        self.setup()
        with self.lock_path.open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def read(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write(self, data: dict) -> None:
        self._atomic_write(self.state_path, data)

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def cmd_setup(store: Store, _args: argparse.Namespace) -> int:
    store.setup()
    print(store.root)
    return 0


def cmd_acquire(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        lease = data.get("lease")
        if lease and parse_time(lease["expires_at"]) > now() and lease["owner"] != args.owner:
            print(json.dumps({"acquired": False, "lease": lease}))
            return 2
        data["lease"] = {
            "owner": args.owner,
            "acquired_at": iso(),
            "expires_at": iso(now() + timedelta(seconds=args.ttl)),
        }
        store.write(data)
        print(json.dumps({"acquired": True, "lease": data["lease"]}))
    return 0


def cmd_release(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        lease = data.get("lease")
        if lease and lease["owner"] == args.owner:
            data["lease"] = None
            store.write(data)
            print(json.dumps({"released": True}))
        else:
            print(json.dumps({"released": False, "lease": lease}))
            return 2
    return 0


def cmd_baseline(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        if data["baseline_complete"] and not args.force:
            print(json.dumps({"changed": False, "count": len(data["records"])}))
            return 0
        for memo_id in args.ids:
            key = str(memo_id)
            data["records"].setdefault(
                key,
                {"status": "baseline", "first_seen_at": iso(), "attempts": 0, "consecutive_failures": 0},
            )
        data["baseline_complete"] = True
        data["initialized_at"] = data["initialized_at"] or iso()
        store.write(data)
        print(json.dumps({"changed": True, "count": len(args.ids)}))
    return 0


def cmd_pending(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        if not data["baseline_complete"]:
            print(json.dumps({"error": "baseline-required"}))
            return 3
        pending = []
        seen = set()
        for memo_id in args.ids:
            key = str(memo_id)
            if key in seen:
                continue
            seen.add(key)
            record = data["records"].get(key)
            if not record or record.get("status") not in {"baseline", "committed", "ignored", "awaiting_review"}:
                pending.append(memo_id)
            if len(pending) >= args.limit:
                break
        print(json.dumps(pending))
    return 0


def cmd_start(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        if record.get("status") in {"baseline", "committed", "awaiting_review"}:
            print(json.dumps({"started": False, "record": record}))
            return 2
        record.update(
            {
                "status": "processing",
                "title": args.title,
                "recorded_at": args.recorded_at,
                "duration": args.duration,
                "last_attempt_at": iso(),
                "attempts": int(record.get("attempts", 0)) + 1,
                "consecutive_failures": int(record.get("consecutive_failures", 0)),
            }
        )
        record.setdefault("first_seen_at", iso())
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_success(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        record.update(
            {
                "status": "committed",
                "commit_sha": args.commit,
                "processed_at": iso(),
                "consecutive_failures": 0,
            }
        )
        for cleanup_key in ("last_error", "last_failure_at", "last_failure_stage"):
            record.pop(cleanup_key, None)
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_review(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        record.update(
            {
                "status": "awaiting_review",
                "review_branch": args.branch,
                "review_commit_sha": args.commit,
                "review_title": args.title,
                "review_affected_notes": args.affected_note,
                "review_queued_at": iso(),
                "consecutive_failures": 0,
            }
        )
        for cleanup_key in ("last_error", "last_failure_at", "last_failure_stage"):
            record.pop(cleanup_key, None)
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_review_pending(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        pending = []
        records = sorted(
            data["records"].items(),
            key=lambda item: item[1].get("review_queued_at", item[1].get("first_seen_at", "")),
        )
        for key, record in records:
            if record.get("status") != "awaiting_review":
                continue
            pending.append(
                {
                    "id": int(key),
                    "branch": record.get("review_branch"),
                    "commit_sha": record.get("review_commit_sha"),
                    "title": record.get("review_title") or record.get("rename_target") or record.get("title"),
                    "affected_notes": record.get("review_affected_notes") or [],
                }
            )
            if len(pending) >= args.limit:
                break
        print(json.dumps(pending))
    return 0


def cmd_rename_queue(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        target_changed = record.get("rename_target") != args.title
        if target_changed:
            record.update(
                {
                    "rename_attempts": 0,
                    "rename_consecutive_failures": 0,
                    "rename_queued_at": iso(),
                    "rename_target": args.title,
                }
            )
            for cleanup_key in ("rename_last_error", "rename_last_failure_at"):
                record.pop(cleanup_key, None)
        record.setdefault("original_title", record.get("title", args.original_title))
        if record.get("renamed_title") == args.title:
            record["rename_status"] = "renamed"
        else:
            record["rename_status"] = "pending"
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_rename_pending(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        pending = []
        records = sorted(
            data["records"].items(),
            key=lambda item: item[1].get("rename_queued_at", item[1].get("first_seen_at", "")),
        )
        for key, record in records:
            if record.get("status") != "committed" or record.get("rename_status") != "pending":
                continue
            pending.append(
                {
                    "id": int(key),
                    "original_title": record.get("original_title", record.get("title")),
                    "target_title": record.get("rename_target"),
                    "recorded_at": record.get("recorded_at"),
                    "duration": record.get("duration"),
                    "attempts": int(record.get("rename_attempts", 0)),
                    "consecutive_failures": int(record.get("rename_consecutive_failures", 0)),
                    "last_error": record.get("rename_last_error"),
                }
            )
            if len(pending) >= args.limit:
                break
        print(json.dumps(pending))
    return 0


def cmd_renamed(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        record.update(
            {
                "rename_status": "renamed",
                "rename_target": args.title,
                "rename_attempts": int(record.get("rename_attempts", 0)) + 1,
                "rename_consecutive_failures": 0,
                "renamed_title": args.title,
                "renamed_at": iso(),
            }
        )
        record.setdefault("original_title", record.get("title", args.original_title))
        for cleanup_key in ("rename_last_error", "rename_last_failure_at"):
            record.pop(cleanup_key, None)
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_rename_fail(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        count = int(record.get("rename_consecutive_failures", 0)) + 1
        attempts = int(record.get("rename_attempts", 0)) + 1
        record.update(
            {
                "rename_status": "pending",
                "rename_attempts": attempts,
                "rename_consecutive_failures": count,
                "rename_last_failure_at": iso(),
                "rename_last_error": args.message[:1000],
            }
        )
        data["records"][key] = record
        store.write(data)
        event = {
            "memo_id": args.id,
            "at": iso(),
            "stage": "rename",
            "message": args.message[:1000],
            "count": count,
        }
        with store.failures.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        print(json.dumps({"record": record, "actionable": count >= 3}))
    return 0


def cmd_fail(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        count = int(record.get("consecutive_failures", 0)) + 1
        record.update(
            {
                "status": "pending",
                "last_failure_at": iso(),
                "last_failure_stage": args.stage,
                "last_error": args.message[:1000],
                "consecutive_failures": count,
            }
        )
        data["records"][key] = record
        store.write(data)
        event = {"memo_id": args.id, "at": iso(), "stage": args.stage, "message": args.message[:1000], "count": count}
        with store.failures.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        # Report the first failed import immediately, then one escalation if the
        # same memo reaches three consecutive failures. This avoids silent loss
        # without notifying on every reconciliation retry.
        print(json.dumps({"record": record, "actionable": count in {1, 3}}))
    return 0


def cmd_ignore(store: Store, args: argparse.Namespace) -> int:
    with store.locked():
        data = store.read()
        key = str(args.id)
        record = data["records"].get(key, {})
        record.update(
            {
                "status": "ignored",
                "ignored_at": iso(),
                "ignored_reason": args.reason[:1000],
                "consecutive_failures": 0,
            }
        )
        for cleanup_key in ("last_error", "last_failure_at", "last_failure_stage"):
            record.pop(cleanup_key, None)
        data["records"][key] = record
        store.write(data)
        print(json.dumps(record))
    return 0


def cmd_prune(store: Store, args: argparse.Namespace) -> int:
    cutoff = now() - timedelta(days=args.days)
    removed = []
    store.setup()
    for path in store.transcripts.iterdir():
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed.append(path.name)
    print(json.dumps({"removed": removed}))
    return 0


def cmd_show(store: Store, args: argparse.Namespace) -> int:
    store.setup()
    data = store.read()
    if args.id is not None:
        print(json.dumps(data["records"].get(str(args.id)), indent=2, sort_keys=True))
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repo", type=Path, required=True)
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("setup")
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl", type=int, default=3600)
    release = commands.add_parser("release")
    release.add_argument("--owner", required=True)
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--ids", nargs="*", type=int, required=True)
    baseline.add_argument("--force", action="store_true")
    pending = commands.add_parser("pending")
    pending.add_argument("--ids", nargs="+", type=int, required=True)
    pending.add_argument("--limit", type=int, default=5)
    start = commands.add_parser("start")
    start.add_argument("--id", type=int, required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--recorded-at", required=True)
    start.add_argument("--duration", type=float, default=0.0)
    success = commands.add_parser("success")
    success.add_argument("--id", type=int, required=True)
    success.add_argument("--commit", required=True)
    review = commands.add_parser("review")
    review.add_argument("--id", type=int, required=True)
    review.add_argument("--branch", required=True)
    review.add_argument("--commit", required=True)
    review.add_argument("--title", required=True)
    review.add_argument("--affected-note", action="append", default=[])
    review_pending = commands.add_parser("review-pending")
    review_pending.add_argument("--limit", type=int, default=100)
    renamed = commands.add_parser("renamed")
    renamed.add_argument("--id", type=int, required=True)
    renamed.add_argument("--title", required=True)
    renamed.add_argument("--original-title", required=True)
    rename_queue = commands.add_parser("rename-queue")
    rename_queue.add_argument("--id", type=int, required=True)
    rename_queue.add_argument("--title", required=True)
    rename_queue.add_argument("--original-title", required=True)
    rename_pending = commands.add_parser("rename-pending")
    rename_pending.add_argument("--limit", type=int, default=5)
    rename_fail = commands.add_parser("rename-fail")
    rename_fail.add_argument("--id", type=int, required=True)
    rename_fail.add_argument("--message", required=True)
    fail = commands.add_parser("fail")
    fail.add_argument("--id", type=int, required=True)
    fail.add_argument("--stage", required=True)
    fail.add_argument("--message", required=True)
    ignore = commands.add_parser("ignore")
    ignore.add_argument("--id", type=int, required=True)
    ignore.add_argument("--reason", required=True)
    prune = commands.add_parser("prune")
    prune.add_argument("--days", type=int, default=30)
    show = commands.add_parser("show")
    show.add_argument("--id", type=int)
    return result


COMMANDS = {
    "setup": cmd_setup,
    "acquire": cmd_acquire,
    "release": cmd_release,
    "baseline": cmd_baseline,
    "pending": cmd_pending,
    "start": cmd_start,
    "success": cmd_success,
    "review": cmd_review,
    "review-pending": cmd_review_pending,
    "rename-queue": cmd_rename_queue,
    "rename-pending": cmd_rename_pending,
    "renamed": cmd_renamed,
    "rename-fail": cmd_rename_fail,
    "fail": cmd_fail,
    "ignore": cmd_ignore,
    "prune": cmd_prune,
    "show": cmd_show,
}


def main() -> int:
    args = parser().parse_args()
    store = Store(args.repo)
    return COMMANDS[args.command](store, args)


if __name__ == "__main__":
    sys.exit(main())
