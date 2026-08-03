#!/usr/bin/env python3
"""Deterministic Voice Memo import coordinator with one narrow Codex edit step."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import log
from pathlib import Path
from typing import Any
from urllib.parse import quote

from qualify_transcript import find_matching_phrase
from resolve_journal_date import parse_recorded_at, resolve_journal_date


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_SCRIPT = SCRIPT_DIR / "state.py"
DIFF_VALIDATOR = SCRIPT_DIR / "validate_diff.py"
TITLE_VALIDATOR = SCRIPT_DIR / "validate_title.py"
VOICE_CLI = SCRIPT_DIR / "voice_memo_cli.mjs"
RENAME_CLI = SCRIPT_DIR / "rename_voice_memo.sh"
BLOCKED_SEARCH_PARTS = {".git", ".voice-memo-automation", "attachments", "assets", "images", "transcripts"}
WORD_RE = re.compile(r"[a-zA-Z0-9]{3,}")
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
STOP_WORDS = {
    "about", "after", "again", "also", "and", "because", "before", "being", "could", "for",
    "friday", "from", "have", "into", "just", "memo", "monday", "note", "notes", "saturday",
    "should", "sunday", "that", "the", "their", "there", "these", "they", "this", "thursday",
    "tuesday", "voice", "wednesday", "what", "when", "where", "which", "with", "work",
    "would", "your",
}


class SyncError(RuntimeError):
    def __init__(self, stage: str, message: str, memo_id: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.memo_id = memo_id


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass
class TranscriptResult:
    text: str
    source: str
    cache_hit: bool
    duration_ms: int


@dataclass
class CandidateContext:
    paths: list[str]
    excerpts: list[dict[str, str]]
    graph: list[dict[str, Any]]
    total_characters: int


def wikilink_targets(content: str) -> list[str]:
    return [target.strip() for target in WIKILINK_RE.findall(content) if target.strip()]


def resolve_wikilink(target: str, paths: set[str], source: str | None = None) -> str | None:
    """Resolve a Foam wikilink to one unambiguous Markdown path."""
    normalized = target.strip().replace("\\", "/")
    if not normalized:
        return None
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    if normalized.startswith("/"):
        normalized = normalized.lstrip("/")
    elif normalized.startswith("."):
        if not source:
            return None
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), normalized))
    normalized = normalized.strip("/")
    if not normalized or normalized == ".." or normalized.startswith("../"):
        return None
    matches = [
        path for path in paths
        if path.removesuffix(".md") == normalized
        or path.removesuffix(".md").endswith("/" + normalized)
    ]
    return matches[0] if len(matches) == 1 else None


def path_has_symlink(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def contained_note_path(root: Path, relative: Path, *, must_exist: bool) -> Path | None:
    if relative.is_absolute() or ".." in relative.parts or path_has_symlink(root, relative):
        return None
    destination = root / relative
    if must_exist and not destination.is_file():
        return None
    try:
        destination.resolve(strict=must_exist).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
    return destination


def contained_directory_path(root: Path, relative: Path) -> Path | None:
    if relative.is_absolute() or ".." in relative.parts or path_has_symlink(root, relative):
        return None
    destination = root / relative
    try:
        destination.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
    return destination if not destination.exists() or destination.is_dir() else None


def eligible_markdown_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    folded_parts = {part.casefold() for part in relative.parts}
    return bool(
        path.suffix.casefold() == ".md"
        and not path_has_symlink(root, relative)
        and contained_note_path(root, relative, must_exist=True)
        and not any(part in BLOCKED_SEARCH_PARTS for part in folded_parts)
        and not any(part.startswith(".") for part in relative.parts)
        and not any("transcript" in part for part in folded_parts)
        and path.name.casefold() != "mac-recorder-transcriber.md"
    )


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    log_fields: dict[str, Any] | None = None,
    timeout_seconds: float | None = 120,
) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        detail = f"command timed out after {timeout_seconds:g} seconds: {args[0]}"
        stderr = f"{stderr.rstrip()}\n{detail}".strip()
        returncode = 124
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            previous = log_path.with_suffix(log_path.suffix + ".1")
            previous.unlink(missing_ok=True)
            log_path.replace(previous)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(),
                "event": "command-completed",
                "argv": args,
                "cwd": str(cwd) if cwd else None,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "exit_code": returncode,
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
                **(log_fields or {}),
            }, sort_keys=True) + "\n")
    result = CommandResult(stdout, stderr, returncode)
    if check and returncode:
        detail = (stderr or stdout).strip()
        raise RuntimeError(detail or f"command exited {returncode}: {args[0]}")
    return result


def json_command(args: list[str], **kwargs: Any) -> Any:
    result = run(args, **kwargs)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON from {args[0]}: {error}") from error


class Coordinator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.repo = args.repo.resolve()
        self.repository_validated = False
        self.state_root = self.repo / ".voice-memo-automation"
        self.transcripts = self.state_root / "transcripts"
        self.worktrees = self.state_root / "worktrees"
        self.log_path = self.state_root / "sync.log"
        # Do not create or rewrite state under an invalid checkout. A missing
        # notes repository is an installation failure, not a fresh vault.
        self.run_id = args.run_id or uuid.uuid4().hex
        self.owner = f"sync-{os.getpid()}-{self.run_id[:8]}"
        self.lease_acquired = False
        self.memos: list[dict[str, Any]] = []
        self.memo_by_id: dict[int, dict[str, Any]] = {}
        self.started_at_wall = datetime.now(timezone.utc)
        self.result: dict[str, Any] = {
            "ok": True,
            "no_op": True,
            "imports": [],
            "reviews": [],
            "actionable_failures": [],
            "ignored_count": 0,
            "run_id": self.run_id,
            "metrics": {
                "codex_calls": 0,
                "started_at": self.started_at_wall.isoformat().replace("+00:00", "Z"),
                "detected_at": args.detected_at,
                "trigger_files": len(args.recording_file),
                "queue": {},
                "stages_ms": {},
                "memos": [],
            },
        }
        self.started = time.monotonic()
        self.semantic_model = ""
        self.semantic_reasoning_effort = "medium"
        self.publish_mode = "review"
        self.review_branch_prefix = "voice-memo/review"
        self.command_timeout_seconds = 120.0
        self.semantic_timeout_seconds = 180.0
        self.transcript_max_characters = 50000
        self.vault_map_max_files = 1000
        self.vault_map_total_characters = 50000
        self.candidate_graph_total_characters = 12000
        self.semantic_prompt_max_characters = 120000

    def demo_progress(self, event: str) -> None:
        if self.args.demo_progress and self.args.recording_file:
            print(f"voice-memo-demo:{event}", flush=True)

    def sanitize_legacy_log(self) -> None:
        if not self.log_path.is_file():
            return
        kept: list[str] = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if "event" in payload and (payload.get("event") != "command-completed" or payload.get("run_id")):
                kept.append(json.dumps(payload, sort_keys=True))
        temporary = self.log_path.with_suffix(f".tmp-{os.getpid()}")
        temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temporary, self.log_path)

    def emit(self, event: str, **fields: Any) -> None:
        if not self.repository_validated:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            "run_id": self.run_id,
            **fields,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    @contextmanager
    def stage(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            stages = self.result["metrics"]["stages_ms"]
            stages[name] = int(stages.get(name, 0)) + elapsed
            self.emit("stage-completed", stage=name, duration_ms=elapsed)

    def state(self, command: str, *arguments: str, check: bool = True) -> Any:
        command_args = [sys.executable, str(STATE_SCRIPT), "--repo", str(self.repo), command, *arguments]
        completed = run(command_args, check=check)
        if not completed.stdout.strip():
            return None
        if command == "setup":
            return completed.stdout.strip()
        return json.loads(completed.stdout)

    def voice(self, command: str, *arguments: str) -> Any:
        return json_command([self.args.node_path, str(self.args.voice_memo_cli), command, *arguments])

    def acquire(self) -> bool:
        acquired = self.state("acquire", "--owner", self.owner, "--ttl", str(self.args.lease_ttl), check=False)
        self.lease_acquired = bool(acquired and acquired.get("acquired"))
        return self.lease_acquired

    def require_repository_checkout(self, memo_id: int | None = None) -> None:
        completed = run(
            ["git", "-C", str(self.repo), "rev-parse", "--show-toplevel"],
            check=False,
        )
        try:
            checkout_root = Path(completed.stdout.strip()).resolve()
        except (OSError, RuntimeError):
            checkout_root = Path()
        if completed.returncode or checkout_root != self.repo:
            raise SyncError(
                "git-preflight",
                f"dedicated notes checkout is not a Git repository: {self.repo}",
                memo_id,
            )
        if not self.repository_validated:
            self.repository_validated = True
            self.sanitize_legacy_log()

    def release(self) -> None:
        if self.lease_acquired:
            self.state("release", "--owner", self.owner, check=False)
            self.lease_acquired = False

    def require_clean_checkout(self, memo_id: int | None = None) -> None:
        self.require_repository_checkout(memo_id)
        if run(["git", "-C", str(self.repo), "status", "--porcelain"]).stdout.strip():
            raise SyncError("git-preflight", "dedicated notes checkout is dirty", memo_id)
        branch = run(["git", "-C", str(self.repo), "branch", "--show-current"]).stdout.strip()
        if branch != self.args.branch:
            raise SyncError(
                "git-preflight", f"expected branch {self.args.branch}, found {branch or 'detached HEAD'}", memo_id
            )

    def sync_checkout(self, memo_id: int | None = None) -> None:
        self.require_clean_checkout(memo_id)
        try:
            run(["git", "-C", str(self.repo), "fetch", "origin", self.args.branch])
            counts = run([
                "git", "-C", str(self.repo), "rev-list", "--left-right", "--count",
                f"origin/{self.args.branch}...HEAD",
            ]).stdout.split()
            remote_only, local_only = map(int, counts)
            if local_only:
                raise SyncError(
                    "git-preflight", "dedicated checkout has unpushed or divergent commits", memo_id
                )
            if remote_only:
                run(["git", "-C", str(self.repo), "merge", "--ff-only", f"origin/{self.args.branch}"])
        except SyncError:
            raise
        except RuntimeError as error:
            raise SyncError("git-preflight", str(error), memo_id) from error

    def list_memos(self) -> None:
        payload = self.voice("list")
        self.memos = sorted(payload["memos"], key=lambda memo: (memo.get("date") or "", int(memo["id"])))
        self.memo_by_id = {int(memo["id"]): memo for memo in self.memos}

    def wait_for_trigger_readiness(self, config: dict[str, Any]) -> None:
        if not self.args.recording_file:
            self.list_memos()
            return
        files = [Path(path) for path in self.args.recording_file]
        deadline = time.monotonic() + float(config["readiness_timeout_seconds"])
        stable_needed = max(1, int(config["readiness_stable_checks"]))
        previous: dict[str, tuple[int, int]] = {}
        stable = 0
        while time.monotonic() < deadline:
            current: dict[str, tuple[int, int]] = {}
            all_present = True
            for path in files:
                try:
                    stat = path.stat()
                    current[str(path)] = (stat.st_size, stat.st_mtime_ns)
                    all_present = all_present and stat.st_size > 0
                except OSError:
                    all_present = False
            stable = stable + 1 if all_present and current == previous else 0
            previous = current
            if stable >= stable_needed:
                try:
                    self.list_memos()
                except Exception:
                    self.emit("readiness-list-retry")
                    time.sleep(1)
                    continue
                database_paths = {Path(str(memo.get("path") or "")).name for memo in self.memos}
                if all(path.name in database_paths for path in files):
                    self.emit("trigger-ready", files=len(files), stable_checks=stable)
                    return
            time.sleep(1)
        raise SyncError("readiness", "new recording did not become stable and visible in the Voice Memos database")

    def load_config(self) -> dict[str, Any]:
        self.state("setup")
        return json.loads((self.state_root / "config.json").read_text(encoding="utf-8"))

    def recover_marker(self, memo_id: int) -> str | None:
        self.sync_checkout(memo_id)
        marker = f"<!-- voice-memo-id:{memo_id} -->"
        matches = []
        for path in self.repo.rglob("*.md"):
            if not eligible_markdown_path(self.repo, path):
                continue
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                matches.append(path.relative_to(self.repo))
        if len(matches) > 1:
            raise SyncError("idempotency", f"memo {memo_id} has duplicate provenance markers", memo_id)
        if not matches:
            return None
        commit = run([
            "git", "-C", str(self.repo), "log", "-1", "--format=%H", "-S", marker, "--", str(matches[0])
        ]).stdout.strip()
        if not commit or run([
            "git", "-C", str(self.repo), "merge-base", "--is-ancestor", commit, f"origin/{self.args.branch}"
        ], check=False).returncode:
            raise SyncError("idempotency", f"memo {memo_id} marker is not backed by origin/{self.args.branch}", memo_id)
        self.state("success", "--id", str(memo_id), "--commit", commit)
        return commit

    def transcript_for(self, memo: dict[str, Any], language: str) -> TranscriptResult:
        started = time.monotonic()
        path = self.transcripts / f"{memo['id']}.txt"
        if path.is_file() and path.stat().st_size:
            cached = path.read_text(encoding="utf-8")
            if len(cached) > self.transcript_max_characters:
                raise SyncError(
                    "transcription",
                    f"cached transcript exceeds configured character limit ({len(cached)} > {self.transcript_max_characters})",
                    int(memo["id"]),
                )
            return TranscriptResult(
                cached, "local-cache", True,
                round((time.monotonic() - started) * 1000),
            )
        payload = self.voice("transcript", "--id", str(memo["id"]), "--language", language)
        text = payload.get("text", "").strip()
        if not text:
            raise SyncError("transcription", "transcription was empty", int(memo["id"]))
        if len(text) > self.transcript_max_characters:
            raise SyncError(
                "transcription",
                f"transcript exceeds configured character limit ({len(text)} > {self.transcript_max_characters})",
                int(memo["id"]),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".tmp-{os.getpid()}")
        temp.write_text(text + "\n", encoding="utf-8")
        os.replace(temp, path)
        return TranscriptResult(
            text, str(payload.get("source") or "apple-speech"), False,
            round((time.monotonic() - started) * 1000),
        )

    def retry_renames(self, limit: int) -> None:
        for item in self.state("rename-pending", "--limit", str(limit)):
            memo_id = int(item["id"])
            memo = self.memo_by_id.get(memo_id)
            if not memo:
                self.rename_failed(memo_id, "recording is not present in the Voice Memos database")
                continue
            self.attempt_rename(
                memo, item["target_title"], item.get("original_title") or memo["title"],
                item.get("recorded_at"), item.get("duration"),
            )

    def rename_failed(self, memo_id: int, message: str) -> None:
        outcome = self.state("rename-fail", "--id", str(memo_id), "--message", message)
        if outcome.get("actionable"):
            self.result["actionable_failures"].append({"memo_id": memo_id, "stage": "rename", "message": message})

    def attempt_rename(
        self,
        memo: dict[str, Any],
        target: str,
        original: str,
        recorded_at: str | None = None,
        duration: float | None = None,
    ) -> str:
        memo_id = int(memo["id"])
        started = time.monotonic()
        status = "pending"
        try:
            if memo.get("title") == target:
                self.state("renamed", "--id", str(memo_id), "--title", target, "--original-title", original)
                status = "renamed"
                return "renamed"
            collision = next((item for item in self.memos if int(item["id"]) != memo_id and item.get("title") == target), None)
            if collision:
                self.rename_failed(memo_id, f"title already belongs to memo {collision['id']}")
                return "pending"
            rename_args = [
                str(self.args.rename_cli), "--memo-id", str(memo_id), "--current-title", str(memo.get("title") or original),
                "--new-title", target,
            ]
            if recorded_at:
                rename_args.extend(["--recorded-at", str(recorded_at)])
            if duration:
                rename_args.extend(["--duration", str(duration)])
            completed = run(
                rename_args, check=False, log_path=self.log_path,
                log_fields={"run_id": self.run_id, "stage": "rename", "memo_id": memo_id},
            )
            try:
                verified = self.voice("get", "--id", str(memo_id))
            except Exception as error:
                verified = None
                verify_error = str(error)
            if verified and verified.get("title") == target:
                self.state("renamed", "--id", str(memo_id), "--title", target, "--original-title", original)
                memo["title"] = target
                status = "renamed"
                return "renamed"
            message = (completed.stderr or completed.stdout).strip()
            if not message and verified is None:
                message = verify_error
            self.rename_failed(memo_id, message or "Voice Memos title verification failed")
            return "pending"
        except Exception as error:
            try:
                self.rename_failed(memo_id, str(error))
            except Exception:
                pass
            return "pending"
        finally:
            self.emit(
                "rename-completed",
                memo_id=memo_id,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

    def candidate_notes(
        self,
        transcript: str,
        journal_date: str,
        limit: int,
        excerpt_characters: int,
        total_characters: int,
    ) -> CandidateContext:
        terms = {word.casefold() for word in WORD_RE.findall(transcript) if word.casefold() not in STOP_WORDS}
        scored: list[tuple[float, str]] = []
        note_contents: dict[str, str] = {}
        path_terms_by_note: dict[str, set[str]] = {}
        content_terms_by_note: dict[str, Counter[str]] = {}
        for path in self.repo.rglob("*.md"):
            if not eligible_markdown_path(self.repo, path):
                continue
            relative = path.relative_to(self.repo)
            relative_text = str(relative)
            path_terms = {word.casefold() for word in WORD_RE.findall(str(relative))}
            content = path.read_text(encoding="utf-8", errors="replace")
            note_contents[relative_text] = content
            content_terms = Counter(word.casefold() for word in WORD_RE.findall(content))
            path_terms_by_note[relative_text] = path_terms
            content_terms_by_note[relative_text] = content_terms
        document_frequency = Counter({
            term: sum(
                term in path_terms_by_note[path] or content_terms_by_note[path][term] > 0
                for path in note_contents
            )
            for term in terms
        })
        note_count = len(note_contents)
        for relative_text in note_contents:
            path_terms = path_terms_by_note[relative_text]
            content_terms = content_terms_by_note[relative_text]
            score = sum(
                (8 if term in path_terms else min(content_terms[term], 3))
                * (1 + log((note_count + 1) / (document_frequency[term] + 1)))
                for term in terms
            )
            if score:
                scored.append((score, relative_text))
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidates = [path for _, path in scored[:limit]]
        journal_prefix = f"journal/{journal_date}"
        for path in self.repo.glob(f"{journal_prefix}*.md"):
            relative = str(path.relative_to(self.repo))
            if relative not in candidates:
                candidates.insert(0, relative)
        candidates = candidates[:limit]
        excerpts: list[dict[str, str]] = []
        remaining = total_characters
        for relative in candidates:
            if remaining <= 0:
                break
            content = (self.repo / relative).read_text(encoding="utf-8", errors="replace")
            cap = min(excerpt_characters, remaining)
            if len(content) <= cap:
                excerpt = content
            else:
                lines = content.splitlines()
                selected: set[int] = set(range(min(16, len(lines))))
                for index, line in enumerate(lines):
                    line_terms = {word.casefold() for word in WORD_RE.findall(line)}
                    if terms.intersection(line_terms):
                        selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
                excerpt = "\n".join(lines[index] for index in sorted(selected))[:cap]
            excerpts.append({"path": relative, "excerpt": excerpt})
            remaining -= len(excerpt)
        known_paths = set(note_contents)
        resolved_links: dict[str, list[str]] = {}
        for source, content in note_contents.items():
            resolved_links[source] = sorted({
                resolved for target in wikilink_targets(content)
                if (resolved := resolve_wikilink(target, known_paths, source)) is not None
            })
        graph = []
        for relative in candidates:
            backlinks = sorted(
                source for source, links in resolved_links.items()
                if source != relative and relative in links
            )
            graph.append({
                "path": relative,
                "outbound": resolved_links.get(relative, [])[:8],
                "backlinks": backlinks[:8],
            })
        return CandidateContext(candidates, excerpts, graph, total_characters - remaining)

    def guidance_context(self) -> list[dict[str, str]]:
        paths = [self.repo / ".cursor/rules/foam-notes.mdc"]
        paths.extend(sorted((self.repo / ".foam/templates").glob("*.md"))[:5])
        result = []
        for path in paths:
            try:
                path.resolve(strict=True).relative_to(self.repo.resolve(strict=True))
            except (FileNotFoundError, RuntimeError, ValueError):
                continue
            if path.is_file() and not path.is_symlink():
                result.append({
                    "path": str(path.relative_to(self.repo)),
                    "excerpt": path.read_text(encoding="utf-8", errors="replace")[:4000],
                })
        return result

    def vault_map(self, priority_paths: list[str]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        eligible = {
            str(path.relative_to(self.repo)): path
            for path in self.repo.rglob("*.md")
            if eligible_markdown_path(self.repo, path)
        }
        ordered = list(dict.fromkeys([*priority_paths, *sorted(eligible)]))
        used_characters = 0
        for relative_text in ordered:
            path = eligible.get(relative_text)
            if path is None:
                continue
            title = ""
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            item = {"path": relative_text, "title": title[:120]}
            item_size = len(json.dumps(item, ensure_ascii=True))
            if len(result) >= self.vault_map_max_files or used_characters + item_size > self.vault_map_total_characters:
                break
            result.append(item)
            used_characters += item_size
        return result

    @staticmethod
    def capped_items(items: list[dict[str, Any]], character_limit: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        used = 0
        for item in items:
            size = len(json.dumps(item, ensure_ascii=True))
            if used + size > character_limit:
                break
            result.append(item)
            used += size
        return result

    def semantic_prompt(
        self,
        memo: dict[str, Any],
        transcript: str,
        matched_phrase: str,
        journal_date: str,
        candidates: CandidateContext,
        existing_title: str | None,
    ) -> str:
        title_rule = f"Reuse this interrupted-run title exactly: {existing_title}" if existing_title else (
            "Create a specific 3-8 word title (maximum 60 characters). Omit routing phrases, filler, and generic labels."
        )
        prompt = f"""You are a planning step inside a deterministic Voice Memo importer. You have no vault access and must not use tools. Return only the supplied JSON schema.

{title_rule}

Plan concise Markdown additions containing facts, decisions, ideas, and only explicitly stated actionable tasks. Do not paste the raw transcript. Never turn a preference, uncertainty, expectation, observation, or implied possibility into a checkbox or follow-up. Add an action item only when the speaker explicitly states a task, request, commitment, reminder, or next step; do not invent actions such as confirming, checking, scheduling, or following up. The routing phrase `{matched_phrase}` is metadata and must be omitted unless independently meaningful. Use `{journal_date}` as the authoritative journal date; never create a weekend journal. Include exactly one marker across all edits: `<!-- voice-memo-id:{memo['id']} -->`.

Treat this Foam vault as a linked thinking graph, not a filing cabinet:
- prefer an existing person, project, decision, meeting, or concept note when the memo extends that subject;
- use the journal as the capture layer for dated tasks, reminders, and low-confidence placement, not as the default home for durable concepts;
- create a durable note only when the memo contains one distinct, reusable subject; keep that note atomic and concept-oriented;
- never create an orphan: every new non-journal note must connect to at least one supplied existing note with a resolvable `[[wikilink]]`, or be linked from an existing note in another edit;
- use map/index/MOC notes only as navigation: add a concise link when a new durable note materially belongs there, and do not duplicate the note body into the map;
- prefer associative links that explain a real relationship. Do not add decorative, speculative, ambiguous, or unresolved links.

Use the supplied capped vault map to understand available subjects, candidate excerpts for content-level placement, and candidate graph context for existing relationships. Use `append` only for an existing candidate path. Use `create` only for a new Markdown note or the authoritative journal path. Preserve existing content. Return 1-5 edits. Set confidence to `low` if the supplied evidence does not clearly support a project/topic note; low-confidence plans must write only to the authoritative journal.

Memo metadata:
- id: {memo['id']}
- current title: {memo.get('title', '')}
- recorded at: {memo.get('date', '')}
- journal date: {journal_date}

Qualified transcript:
<transcript>
{transcript}
</transcript>

Foam guidance excerpts:
{json.dumps(self.guidance_context(), ensure_ascii=True)}

Capped vault map (paths and headings only):
{json.dumps(self.vault_map(candidates.paths), ensure_ascii=True)}

Candidate note excerpts:
{json.dumps(candidates.excerpts, ensure_ascii=True)}

Candidate graph context (resolved Foam links only):
{json.dumps(self.capped_items(candidates.graph, self.candidate_graph_total_characters), ensure_ascii=True)}
"""
        if len(prompt) > self.semantic_prompt_max_characters:
            raise SyncError(
                "semantic-context",
                f"semantic prompt exceeds configured character limit ({len(prompt)} > {self.semantic_prompt_max_characters})",
                int(memo["id"]),
            )
        return prompt

    @staticmethod
    def codex_metrics(stdout: str) -> dict[str, int]:
        metrics = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "tool_calls": 0}
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in metrics and key != "tool_calls" and isinstance(item, (int, float)):
                        metrics[key] = max(metrics[key], int(item))
                    if key in {"tool", "tool_name", "command"}:
                        metrics["tool_calls"] += 1
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        for line in stdout.splitlines():
            try:
                walk(json.loads(line))
            except json.JSONDecodeError:
                continue
        metrics["total_tokens"] = max(
            metrics["total_tokens"],
            metrics["input_tokens"] + metrics["output_tokens"],
        )
        return metrics

    def call_codex(self, prompt: str, memo_id: int) -> tuple[dict[str, Any], dict[str, int]]:
        self.result["metrics"]["codex_calls"] += 1
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "placement_reason": {"type": "string"},
                "edits": {
                    "type": "array", "minItems": 1, "maxItems": 5,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "mode": {"type": "string", "enum": ["append", "create"]},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "mode", "content"],
                    },
                },
            },
            "required": ["title", "summary", "confidence", "placement_reason", "edits"],
        }
        output_dir = self.state_root / "runs"
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"memo-{memo_id}-", dir=output_dir) as directory:
            semantic_directory = Path(directory)
            schema_path = semantic_directory / "schema.json"
            result_path = semantic_directory / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            semantic_environment = os.environ.copy()
            semantic_environment.pop("VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE", None)
            started = time.monotonic()
            codex_args = [
                self.args.codex_path, "-a", "never", "exec", "--ephemeral", "--json",
                "-C", str(semantic_directory), "--skip-git-repo-check",
                "--ignore-user-config", "--ignore-rules",
                "-s", "read-only", "-c", f'model_reasoning_effort="{self.semantic_reasoning_effort}"',
                "--output-schema", str(schema_path), "-o", str(result_path), "-",
            ]
            if self.semantic_model:
                codex_args[10:10] = ["-m", self.semantic_model]
            completed = run(
                codex_args,
                input_text=prompt,
                check=False,
                env=semantic_environment,
                log_path=self.log_path,
                log_fields={"run_id": self.run_id, "stage": "codex", "memo_id": memo_id},
                timeout_seconds=self.semantic_timeout_seconds,
            )
            metrics = self.codex_metrics(completed.stdout)
            metrics["duration_ms"] = round((time.monotonic() - started) * 1000)
            if completed.returncode:
                raise SyncError("semantic-edit", completed.stderr.strip() or f"Codex exited {completed.returncode}", memo_id)
            try:
                return json.loads(result_path.read_text(encoding="utf-8")), metrics
            except (OSError, json.JSONDecodeError) as error:
                raise SyncError("semantic-edit", f"Codex returned invalid structured output: {error}", memo_id) from error

    def apply_plan(
        self,
        worktree: Path,
        output: dict[str, Any],
        candidates: CandidateContext,
        journal_date: str,
        memo_id: int,
    ) -> list[str]:
        edits = output.get("edits") or []
        if not 1 <= len(edits) <= 5:
            raise SyncError("validation", "semantic plan must contain 1-5 edits", memo_id)
        allowed_existing = set(candidates.paths)
        journal_pattern = re.compile(rf"^journal/{re.escape(journal_date)}(?:-[^/]+)?\.md$")
        if output.get("confidence") == "low" and any(not journal_pattern.match(str(edit.get("path") or "")) for edit in edits):
            raise SyncError("validation", "low-confidence semantic plans may only target the journal", memo_id)
        existing_paths: set[str] = set()
        for path in worktree.rglob("*.md"):
            if not eligible_markdown_path(worktree, path):
                continue
            existing_paths.add(str(path.relative_to(worktree)))
        created_paths = {
            str(edit.get("path") or "") for edit in edits if str(edit.get("mode") or "") == "create"
        }
        known_paths = existing_paths | created_paths
        resolved_by_edit: dict[str, list[str]] = {}
        for edit in edits:
            relative = str(edit.get("path") or "")
            resolved_by_edit[relative] = []
            for target in wikilink_targets(str(edit.get("content") or "")):
                resolved = resolve_wikilink(target, known_paths, relative)
                if resolved is None:
                    raise SyncError("validation", f"unresolved or ambiguous Foam wikilink: {target}", memo_id)
                resolved_by_edit[relative].append(resolved)
        for edit in edits:
            relative = str(edit.get("path") or "")
            if str(edit.get("mode") or "") != "create" or journal_pattern.match(relative):
                continue
            links_existing = any(target in existing_paths for target in resolved_by_edit.get(relative, []))
            linked_from_existing = any(
                str(source.get("mode") or "") == "append"
                and relative in resolved_by_edit.get(str(source.get("path") or ""), [])
                for source in edits
            )
            if not links_existing and not linked_from_existing:
                raise SyncError("validation", f"new durable note would be orphaned: {relative}", memo_id)
        changed: list[str] = []
        for edit in edits:
            relative = str(edit.get("path") or "")
            mode = str(edit.get("mode") or "")
            content = str(edit.get("content") or "")
            path = Path(relative)
            folded_parts = {part.casefold() for part in path.parts}
            if (
                not relative or path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".md"
                or any(part.startswith(".") for part in path.parts)
                or any(part in BLOCKED_SEARCH_PARTS for part in folded_parts)
                or any("transcript" in part for part in folded_parts)
                or path.name.casefold() == "mac-recorder-transcriber.md"
                or relative in changed or not content.strip()
            ):
                raise SyncError("validation", f"unsafe semantic edit path: {relative}", memo_id)
            if mode == "append":
                destination = contained_note_path(worktree, path, must_exist=True)
                if relative not in allowed_existing or destination is None:
                    raise SyncError("validation", f"append target was not an existing candidate: {relative}", memo_id)
                with destination.open("a", encoding="utf-8") as handle:
                    handle.write(("\n" if destination.stat().st_size else "") + content.strip() + "\n")
            elif mode == "create":
                destination = contained_note_path(worktree, path, must_exist=False)
                if (
                    destination is None
                    or destination.exists()
                    or not contained_directory_path(worktree, path.parent)
                    or (not journal_pattern.match(relative) and not re.match(r"^[A-Za-z0-9_ /-]+\.md$", relative))
                ):
                    raise SyncError("validation", f"invalid create target: {relative}", memo_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content.strip() + "\n", encoding="utf-8")
            else:
                raise SyncError("validation", f"unknown edit mode: {mode}", memo_id)
            changed.append(relative)
        return sorted(changed)

    def changed_files(self, worktree: Path) -> list[str]:
        tracked = run(["git", "-C", str(worktree), "diff", "--name-only"]).stdout.splitlines()
        untracked = run(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"]).stdout.splitlines()
        return sorted(set(tracked + untracked))

    def process_qualified(
        self,
        memo: dict[str, Any],
        transcript: str,
        matched_phrase: str,
        journal_date: str,
        record: dict[str, Any],
        config: dict[str, Any],
        memo_metrics: dict[str, Any],
    ) -> str:
        memo_id = int(memo["id"])
        self.demo_progress("organizing")
        git_started = time.monotonic()
        self.sync_checkout(memo_id)
        worktree = self.worktrees / f"memo-{memo_id}-{uuid.uuid4().hex[:8]}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            run(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(worktree), f"origin/{self.args.branch}"])
            memo_metrics["git_prepare_ms"] = round((time.monotonic() - git_started) * 1000)
            retrieval_started = time.monotonic()
            candidates = self.candidate_notes(
                transcript,
                journal_date,
                int(config["candidate_file_limit"]),
                int(config["candidate_excerpt_characters"]),
                int(config["candidate_total_characters"]),
            )
            memo_metrics["retrieval"] = {
                "duration_ms": round((time.monotonic() - retrieval_started) * 1000),
                "candidate_files": len(candidates.excerpts),
                "candidate_characters": candidates.total_characters,
            }
            existing_title = record.get("rename_target")
            self.demo_progress("drafting")
            output, codex_metrics = self.call_codex(
                self.semantic_prompt(memo, transcript, matched_phrase, journal_date, candidates, existing_title),
                memo_id,
            )
            memo_metrics["codex"] = codex_metrics
            memo_metrics["semantic_confidence"] = str(output.get("confidence") or "unknown")

            validation_started = time.monotonic()
            title = str(output.get("title", "")).strip()
            title_args = [sys.executable, str(TITLE_VALIDATOR), "--title", title]
            for phrase in config["required_trigger_phrases"]:
                title_args.extend(["--trigger", phrase])
            title_result = run(title_args, check=False)
            if title_result.returncode:
                raise SyncError("validation", f"invalid generated title: {title_result.stdout.strip()}", memo_id)
            if existing_title and title != existing_title:
                raise SyncError("validation", "Codex changed an already queued title", memo_id)
            collision = next((item for item in self.memos if int(item["id"]) != memo_id and item.get("title") == title), None)
            if collision:
                raise SyncError("validation", f"generated title belongs to memo {collision['id']}", memo_id)

            changed = self.apply_plan(worktree, output, candidates, journal_date, memo_id)
            if self.changed_files(worktree) != changed:
                raise SyncError("validation", "applied edit plan did not match the resulting diff", memo_id)
            self.state("rename-queue", "--id", str(memo_id), "--title", title, "--original-title", str(memo.get("title") or ""))
            run(["git", "-C", str(worktree), "add", "--all", "--", *changed])
            validation = run([
                sys.executable, str(DIFF_VALIDATOR), "--repo", str(worktree), "--memo-id", str(memo_id),
                "--max-files", str(config["max_files_per_memo"]),
            ], check=False)
            if validation.returncode:
                raise SyncError("validation", validation.stdout.strip() or validation.stderr.strip(), memo_id)
            memo_metrics["validation_ms"] = round((time.monotonic() - validation_started) * 1000)
            self.demo_progress("validated")

            publish_started = time.monotonic()
            run(["git", "-C", str(worktree), "commit", "-m", f"Import voice memo {memo_id}: {title}"])
            commit = run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()
            if self.publish_mode == "review":
                review_branch = f"{self.review_branch_prefix.rstrip('/')}-{memo_id}-{commit[:8]}"
                if run(["git", "check-ref-format", "--branch", review_branch], check=False).returncode:
                    raise SyncError("configuration", f"invalid review branch: {review_branch}", memo_id)
                destination_ref = f"refs/heads/{review_branch}"
            else:
                review_branch = None
                destination_ref = f"refs/heads/{self.args.branch}"
            pushed = run(
                ["git", "-C", str(worktree), "push", "origin", f"HEAD:{destination_ref}"],
                check=False,
                timeout_seconds=self.command_timeout_seconds,
            )
            if pushed.returncode:
                raise SyncError("git-push", pushed.stderr.strip() or pushed.stdout.strip(), memo_id)
            memo_metrics["publish_ms"] = round((time.monotonic() - publish_started) * 1000)
            memo_metrics["commit_sha"] = commit
            memo_metrics["affected_file_count"] = len(changed)
            try:
                web_url = self.repository_web_url()
            except Exception:
                web_url = None
            self.result["no_op"] = False
            if review_branch:
                verify = run(
                    ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{review_branch}"],
                    cwd=worktree,
                    check=False,
                    timeout_seconds=self.command_timeout_seconds,
                )
                if verify.returncode or not verify.stdout.startswith(commit):
                    raise SyncError("git-push", "review branch did not retain the expected commit", memo_id)
                state_args = [
                    "review", "--id", str(memo_id), "--branch", review_branch,
                    "--commit", commit, "--title", title,
                ]
                for note in changed:
                    state_args.extend(["--affected-note", note])
                self.state(*state_args)
                review_url = (
                    f"{web_url}/compare/{quote(self.args.branch, safe='')}...{quote(review_branch, safe='')}?expand=1"
                    if web_url else None
                )
                self.result["reviews"].append({
                    "memo_id": memo_id,
                    "title": title,
                    "affected_notes": changed,
                    "commit_sha": commit,
                    "branch": review_branch,
                    "review_url": review_url,
                })
                self.demo_progress("review-ready")
                return "awaiting_review"

            run(["git", "-C", str(worktree), "fetch", "origin", self.args.branch])
            if run([
                "git", "-C", str(worktree), "merge-base", "--is-ancestor", commit, f"origin/{self.args.branch}"
            ], check=False).returncode:
                raise SyncError("git-push", "pushed commit is not on origin branch", memo_id)
            self.state("success", "--id", str(memo_id), "--commit", commit)
            self.result["imports"].append({
                "memo_id": memo_id,
                "title": title,
                "affected_notes": changed,
                "commit_sha": commit,
                "github_url": f"{web_url}/commit/{commit}" if web_url else None,
                "rename_status": "pending",
                "metrics": memo_metrics,
            })
            self.demo_progress("imported")
            run(["git", "-C", str(self.repo), "fetch", "origin", self.args.branch], check=False)
            run(["git", "-C", str(self.repo), "merge", "--ff-only", f"origin/{self.args.branch}"], check=False)
            return "imported"
        finally:
            if worktree.exists():
                run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(worktree)], check=False)

    def repository_web_url(self) -> str | None:
        remote = run(["git", "-C", str(self.repo), "remote", "get-url", "origin"]).stdout.strip()
        if remote.startswith("git@github.com:"):
            return "https://github.com/" + remote.removeprefix("git@github.com:").removesuffix(".git")
        if remote.startswith("https://github.com/"):
            return remote.removesuffix(".git")
        return None

    def reconcile_reviews(self, limit: int) -> None:
        for review in self.state("review-pending", "--limit", str(limit)):
            memo_id = int(review["id"])
            try:
                commit = self.recover_marker(memo_id)
            except SyncError as error:
                self.result["actionable_failures"].append({
                    "memo_id": memo_id,
                    "stage": error.stage,
                    "message": str(error),
                })
                continue
            if not commit:
                continue
            web_url = self.repository_web_url()
            record = self.state("show", "--id", str(memo_id)) or {}
            self.result["imports"].append({
                "memo_id": memo_id,
                "title": review.get("title") or record.get("rename_target") or record.get("title") or "Voice memo",
                "affected_notes": review.get("affected_notes") or [],
                "commit_sha": commit,
                "github_url": f"{web_url}/commit/{commit}" if web_url else None,
                "rename_status": record.get("rename_status") or "pending",
            })
            self.emit("review-merged", memo_id=memo_id, commit_sha=commit)
            self.result["no_op"] = False

    def record_failure(self, error: SyncError) -> None:
        if error.memo_id is None:
            self.result["actionable_failures"].append({"memo_id": None, "stage": error.stage, "message": str(error)})
            self.result["ok"] = False
            return
        outcome = self.state(
            "fail", "--id", str(error.memo_id), "--stage", error.stage, "--message", str(error), check=False
        )
        if outcome and outcome.get("actionable"):
            self.result["actionable_failures"].append({
                "memo_id": error.memo_id, "stage": error.stage, "message": str(error),
            })

    def execute(self) -> dict[str, Any]:
        with self.stage("repository_preflight"):
            self.require_repository_checkout()
        with self.stage("configuration"):
            config = self.load_config()

        def positive(name: str, value_type: type[int] | type[float]) -> int | float:
            try:
                value = value_type(config[name])
            except (KeyError, TypeError, ValueError) as error:
                raise SyncError("configuration", f"{name} must be a positive number") from error
            if value <= 0:
                raise SyncError("configuration", f"{name} must be a positive number")
            return value

        self.args.branch = self.args.branch or str(config["branch"])
        if run(["git", "check-ref-format", "--branch", self.args.branch], check=False).returncode:
            raise SyncError("configuration", f"invalid notes branch: {self.args.branch}")
        self.semantic_model = str(config.get("semantic_model") or "")
        self.semantic_reasoning_effort = str(config["semantic_reasoning_effort"])
        if self.semantic_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise SyncError("configuration", "semantic_reasoning_effort must be low, medium, high, or xhigh")
        self.publish_mode = str(config.get("publish_mode") or "review")
        if self.publish_mode not in {"review", "direct"}:
            raise SyncError("configuration", f"unsupported publish mode: {self.publish_mode}")
        self.review_branch_prefix = str(config.get("review_branch_prefix") or "voice-memo/review")
        if run([
            "git", "check-ref-format", "--branch", f"{self.review_branch_prefix.rstrip('/')}-1-deadbeef",
        ], check=False).returncode:
            raise SyncError("configuration", f"invalid review branch prefix: {self.review_branch_prefix}")
        self.command_timeout_seconds = float(positive("command_timeout_seconds", float))
        self.semantic_timeout_seconds = float(positive("semantic_timeout_seconds", float))
        self.transcript_max_characters = int(positive("transcript_max_characters", int))
        self.vault_map_max_files = int(positive("vault_map_max_files", int))
        self.vault_map_total_characters = int(positive("vault_map_total_characters", int))
        self.candidate_graph_total_characters = int(positive("candidate_graph_total_characters", int))
        self.semantic_prompt_max_characters = int(positive("semantic_prompt_max_characters", int))
        for name in (
            "max_memos_per_run", "max_files_per_memo", "transcript_retention_days",
            "candidate_file_limit", "candidate_excerpt_characters", "candidate_total_characters",
            "readiness_timeout_seconds", "readiness_stable_checks",
        ):
            positive(name, int)
        trigger_phrases = config.get("required_trigger_phrases")
        if not isinstance(trigger_phrases, list) or not trigger_phrases or not all(
            isinstance(phrase, str) and phrase.strip() for phrase in trigger_phrases
        ):
            raise SyncError("configuration", "required_trigger_phrases must be a non-empty string list")
        with self.stage("lease"):
            acquired = self.acquire()
        if not acquired:
            self.result["metrics"]["queue"] = {"lease_blocked": True}
            self.result["metrics"]["duration_ms"] = round((time.monotonic() - self.started) * 1000)
            return self.result
        try:
            with self.stage("git_preflight"):
                self.require_clean_checkout()
            with self.stage("readiness_and_listing"):
                self.wait_for_trigger_readiness(config)
            state = self.state("show")
            ids = [str(memo["id"]) for memo in self.memos]
            self.result["metrics"]["queue"].update({"listed": len(ids)})
            if not state["baseline_complete"]:
                self.state("baseline", "--ids", *ids)
                self.result["metrics"]["queue"]["baselined"] = len(ids)
                self.result["metrics"]["duration_ms"] = round((time.monotonic() - self.started) * 1000)
                return self.result

            with self.stage("review_reconciliation"):
                self.reconcile_reviews(int(config["max_memos_per_run"]))

            if self.args.recording_file:
                self.result["metrics"]["queue"]["rename_retry_deferred"] = True
            else:
                with self.stage("rename_retry"):
                    self.retry_renames(int(config["max_memos_per_run"]))
            if not ids:
                return self.result
            pending = self.state("pending", "--ids", *ids, "--limit", str(config["max_memos_per_run"]))
            self.result["metrics"]["queue"]["pending"] = len(pending)
            for memo_id in pending:
                memo = self.memo_by_id[int(memo_id)]
                memo_started = time.monotonic()
                duration = float(memo.get("duration") or 0)
                memo_metrics: dict[str, Any] = {
                    "memo_id": int(memo_id),
                    "recorded_at": memo.get("date"),
                    "duration_seconds": duration,
                    "detected_at": self.args.detected_at,
                }
                outcome = "pending"
                try:
                    recorded_value = str(memo["date"]).replace("Z", "+00:00")
                    recorded = datetime.fromisoformat(recorded_value)
                    recording_ended = recorded + timedelta(seconds=duration)
                    memo_metrics["recording_ended_at"] = recording_ended.isoformat()
                    if self.args.detected_at:
                        detected = datetime.fromisoformat(self.args.detected_at.replace("Z", "+00:00"))
                        memo_metrics["recording_end_to_detection_ms"] = round(
                            (detected - recording_ended).total_seconds() * 1000
                        )
                        memo_metrics["detection_to_run_ms"] = round(
                            (self.started_at_wall - detected).total_seconds() * 1000
                        )
                except Exception:
                    pass
                trigger = next((
                    Path(path) for path in self.args.recording_file
                    if Path(path).name == Path(str(memo.get("path") or "")).name
                ), None)
                if trigger:
                    try:
                        stat = trigger.stat()
                        created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime), timezone.utc)
                        memo_metrics["file_appeared_at"] = created.isoformat().replace("+00:00", "Z")
                        if self.args.detected_at:
                            detected = datetime.fromisoformat(self.args.detected_at.replace("Z", "+00:00"))
                            memo_metrics["file_appearance_to_detection_ms"] = round(
                                (detected - created).total_seconds() * 1000
                            )
                    except OSError:
                        pass
                try:
                    if self.recover_marker(int(memo_id)):
                        outcome = "recovered"
                        continue
                    record = self.state(
                        "start", "--id", str(memo_id), "--title", str(memo.get("title") or ""),
                        "--recorded-at", str(memo.get("date") or ""),
                        "--duration", str(duration),
                    )
                    self.demo_progress("listening")
                    transcript_result = self.transcript_for(memo, config["language"])
                    memo_metrics["transcription"] = {
                        "source": transcript_result.source,
                        "cache_hit": transcript_result.cache_hit,
                        "duration_ms": transcript_result.duration_ms,
                        "characters": len(transcript_result.text),
                    }
                    qualification_started = time.monotonic()
                    matched = find_matching_phrase(transcript_result.text, config["required_trigger_phrases"])
                    memo_metrics["qualification_ms"] = round((time.monotonic() - qualification_started) * 1000)
                    memo_metrics["qualified"] = bool(matched)
                    if not matched:
                        self.state("ignore", "--id", str(memo_id), "--reason", "missing work trigger")
                        self.result["ignored_count"] += 1
                        outcome = "ignored"
                        self.demo_progress("ignored")
                        continue
                    self.demo_progress("qualified")
                    recorded_date = parse_recorded_at(str(memo["date"]))
                    journal_date = resolve_journal_date(recorded_date).isoformat()
                    outcome = self.process_qualified(
                        memo, transcript_result.text, matched, journal_date, record, config, memo_metrics,
                    )
                except SyncError as error:
                    self.record_failure(error)
                    memo_metrics["failure_stage"] = error.stage
                    outcome = "failed"
                except Exception as error:
                    self.record_failure(SyncError("internal", str(error), int(memo_id)))
                    memo_metrics["failure_stage"] = "internal"
                    outcome = "failed"
                finally:
                    memo_metrics["outcome"] = outcome
                    memo_metrics["duration_ms"] = round((time.monotonic() - memo_started) * 1000)
                    self.result["metrics"]["memos"].append(memo_metrics)
                    self.emit(
                        "memo-metrics",
                        memo_id=int(memo_id),
                        schema_version=2,
                        metrics=memo_metrics,
                    )
                    self.emit(
                        "memo-completed",
                        memo_id=int(memo_id),
                        duration_ms=memo_metrics["duration_ms"],
                        qualified=memo_metrics.get("qualified"),
                        failure_stage=memo_metrics.get("failure_stage"),
                    )
            return self.result
        except SyncError as error:
            self.record_failure(error)
            return self.result
        except Exception as error:
            self.record_failure(SyncError("internal", str(error)))
            return self.result
        finally:
            with self.stage("prune"):
                pruned = self.state("prune", "--days", str(config["transcript_retention_days"]), check=False)
                self.result["metrics"]["pruned_transcripts"] = len((pruned or {}).get("removed", []))
            self.release()
            self.result["metrics"]["duration_ms"] = round((time.monotonic() - self.started) * 1000)
            self.result["metrics"]["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self.emit(
                "run-completed",
                duration_ms=self.result["metrics"]["duration_ms"],
                imports=len(self.result["imports"]),
                reviews=len(self.result["reviews"]),
                ignored=self.result["ignored_count"],
                failures=len(self.result["actionable_failures"]),
            )


def parser() -> argparse.ArgumentParser:
    default_node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").is_file() else (shutil.which("node") or "node")
    result = argparse.ArgumentParser()
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--branch")
    result.add_argument("--codex-path", default=shutil.which("codex") or "codex")
    result.add_argument("--node-path", default=default_node)
    result.add_argument("--voice-memo-cli", type=Path, default=VOICE_CLI)
    result.add_argument("--rename-cli", type=Path, default=RENAME_CLI)
    result.add_argument("--lease-ttl", type=int, default=3600)
    result.add_argument("--run-id")
    result.add_argument("--detected-at")
    result.add_argument("--recording-file", action="append", default=[])
    result.add_argument("--demo-progress", action="store_true")
    result.add_argument("--result-file", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    coordinator = Coordinator(args)
    try:
        result = coordinator.execute()
    except SyncError as error:
        coordinator.record_failure(error)
        result = coordinator.result
    except Exception as error:
        coordinator.record_failure(SyncError("internal", str(error)))
        result = coordinator.result
    if not result["metrics"].get("duration_ms"):
        result["metrics"]["duration_ms"] = round((time.monotonic() - coordinator.started) * 1000)
        result["metrics"]["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        coordinator.emit(
            "run-completed",
            duration_ms=result["metrics"]["duration_ms"],
            imports=len(result["imports"]),
            reviews=len(result["reviews"]),
            ignored=result["ignored_count"],
            failures=len(result["actionable_failures"]),
        )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.result_file:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.result_file.parent, delete=False) as handle:
            handle.write(encoded)
            temporary = Path(handle.name)
        os.replace(temporary, args.result_file)
    else:
        sys.stdout.write(encoded)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
