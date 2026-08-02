#!/usr/bin/env python3
"""Build a privacy-safe benchmark report from durable Voice Memo telemetry."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CORE_METRICS = {
    "run_ms": ("timings_ms", "run"),
    "detection_to_notification_ms": ("timings_ms", "detection_to_notification"),
    "transcription_ms": ("timings_ms", "transcription"),
    "retrieval_ms": ("timings_ms", "retrieval"),
    "codex_ms": ("timings_ms", "codex"),
    "validation_ms": ("timings_ms", "validation"),
    "publish_ms": ("timings_ms", "publish"),
    "total_tokens": ("codex", "total_tokens"),
    "tool_calls": ("codex", "tool_calls"),
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def elapsed_ms(start: str | None, end: str | None) -> int | None:
    start_time = parse_time(start)
    end_time = parse_time(end)
    if not start_time or not end_time:
        return None
    if (start_time.tzinfo is None) != (end_time.tzinfo is None):
        return None
    return round((end_time - start_time).total_seconds() * 1000)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def nested(report: dict[str, Any], path: tuple[str, str]) -> int | float | None:
    value = report.get(path[0], {}).get(path[1])
    return value if isinstance(value, (int, float)) else None


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def configured_branch(repo: Path, requested: str | None) -> str:
    if requested:
        return requested
    config_path = repo / ".voice-memo-automation" / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        branch = str(config.get("branch") or "").strip()
        if branch:
            return branch
    current = git(repo, "branch", "--show-current").stdout.strip()
    if current:
        return current
    raise RuntimeError("cannot determine the notes branch; pass --branch")


def correctness(repo: Path, branch: str, memo_id: int, metrics: dict[str, Any]) -> dict[str, Any]:
    commit = str(metrics.get("commit_sha") or "")
    if not commit:
        return {"available": False, "reason": "commit SHA was not retained"}
    remote_ref = f"origin/{branch}"
    remote = git(repo, "merge-base", "--is-ancestor", commit, remote_ref).returncode == 0
    numstat = git(repo, "show", "--format=", "--numstat", commit).stdout.splitlines()
    additions = 0
    deletions = 0
    files = 0
    for line in numstat:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        files += 1
        if parts[0].isdigit():
            additions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    diff = git(repo, "show", "--format=", "--unified=0", commit).stdout
    marker = f"voice-memo-id:{memo_id}"
    marker_additions = sum(1 for line in diff.splitlines() if line.startswith("+") and marker in line)
    return {
        "available": True,
        "affected_files": files,
        "additions": additions,
        "checkout_clean": not bool(git(repo, "status", "--porcelain").stdout.strip()),
        "deletions": deletions,
        "marker_additions": marker_additions,
        "remote_branch": remote_ref,
        "remote_contains_commit": remote,
        "valid": remote and deletions == 0 and marker_additions == 1 and 1 <= files <= 5,
    }


def build_report(
    repo: Path,
    branch: str,
    requested_run: str | None,
    requested_memo: int | None,
) -> dict[str, Any]:
    state_root = repo / ".voice-memo-automation"
    sync_events = read_jsonl(state_root / "sync.log")
    agent_events = read_jsonl(state_root / "agent.log")
    if requested_run:
        run_id = requested_run
    else:
        completions = [
            item for item in sync_events
            if item.get("event") == "run-completed" and int(item.get("imports") or 0) > 0
        ]
        if not completions:
            raise RuntimeError("no completed import run is present in sync.log")
        run_id = str(completions[-1]["run_id"])

    run_events = [item for item in sync_events if item.get("run_id") == run_id]
    memo_events = [item for item in run_events if item.get("event") == "memo-metrics"]
    if not memo_events:
        merged = next((item for item in reversed(run_events) if item.get("event") == "review-merged"), None)
        if merged:
            merged_memo_id = int(merged.get("memo_id") or -1)
            merged_commit = str(merged.get("commit_sha") or "")
            memo_events = [
                item for item in sync_events
                if item.get("event") == "memo-metrics"
                and int(item.get("memo_id") or -1) == merged_memo_id
                and str((item.get("metrics") or {}).get("commit_sha") or "") == merged_commit
            ]
    if requested_memo is not None:
        memo_events = [item for item in memo_events if int(item.get("memo_id") or -1) == requested_memo]
    if not memo_events:
        raise RuntimeError(
            f"run {run_id} has no durable memo-metrics event; select a run created after telemetry schema 2"
        )
    memo_event = memo_events[-1]
    metrics = memo_event.get("metrics") or {}
    memo_id = int(memo_event["memo_id"])
    run_completed = next((item for item in reversed(run_events) if item.get("event") == "run-completed"), {})
    stage_timings = {
        str(item["stage"]): int(item["duration_ms"])
        for item in run_events
        if item.get("event") == "stage-completed" and item.get("stage") and item.get("duration_ms") is not None
    }
    app_events = [item for item in agent_events if item.get("run_id") == run_id]
    notification = next((
        item for item in reversed(app_events)
        if item.get("event") == "notification-sent"
        and item.get("kind", "import") == "import"
        and int(item.get("memo_id") or -1) == memo_id
    ), {})
    started = next((item for item in app_events if item.get("event") == "sync-started"), {})
    recording = next((item for item in app_events if item.get("event") == "recording-created"), {})
    detected_at = str(metrics.get("detected_at") or recording.get("detected_at") or recording.get("at") or "") or None
    notification_at = str(notification.get("at") or "") or None
    recording_end = str(metrics.get("recording_ended_at") or "") or None
    parsed_recording_end = parse_time(recording_end)
    recorded_at = parse_time(str(metrics.get("recorded_at") or ""))
    if (not parsed_recording_end or parsed_recording_end.tzinfo is None) and recorded_at:
        recording_end = (
            recorded_at + timedelta(seconds=float(metrics.get("duration_seconds") or 0))
        ).isoformat()

    transcription = metrics.get("transcription") or {}
    retrieval = metrics.get("retrieval") or {}
    codex = metrics.get("codex") or {}
    input_tokens = int(codex.get("input_tokens") or 0)
    output_tokens = int(codex.get("output_tokens") or 0)
    total_tokens = max(int(codex.get("total_tokens") or 0), input_tokens + output_tokens)
    report = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
        "memo_id": memo_id,
        "outcome": metrics.get("outcome"),
        "timings_ms": {
            "codex": codex.get("duration_ms"),
            "detection_to_notification": elapsed_ms(detected_at, notification_at),
            "detection_to_run": metrics.get("detection_to_run_ms") or elapsed_ms(detected_at, started.get("at")),
            "file_appearance_to_detection": metrics.get("file_appearance_to_detection_ms"),
            "git_prepare": metrics.get("git_prepare_ms"),
            "memo": metrics.get("duration_ms"),
            "notification_delivery": notification.get("duration_ms"),
            "publish": metrics.get("publish_ms"),
            "qualification": metrics.get("qualification_ms"),
            "recording_end_to_detection": metrics.get("recording_end_to_detection_ms"),
            "recording_end_to_notification": elapsed_ms(recording_end, notification_at),
            "retrieval": retrieval.get("duration_ms"),
            "run": run_completed.get("duration_ms"),
            "transcription": transcription.get("duration_ms"),
            "validation": metrics.get("validation_ms"),
            **{f"stage_{key}": value for key, value in stage_timings.items()},
        },
        "codex": {
            "cached_input_tokens": codex.get("cached_input_tokens"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "semantic_confidence": metrics.get("semantic_confidence"),
            "tool_calls": codex.get("tool_calls"),
            "total_tokens": total_tokens,
        },
        "transcription": {
            "cache_hit": transcription.get("cache_hit"),
            "characters": transcription.get("characters"),
            "source": transcription.get("source"),
        },
        "retrieval": {
            "candidate_characters": retrieval.get("candidate_characters"),
            "candidate_files": retrieval.get("candidate_files"),
        },
        "delivery": {
            "notification_sent": bool(notification),
            "provider": notification.get("provider"),
        },
    }
    report["correctness"] = correctness(repo, branch, memo_id, metrics)
    return report


def add_comparison(report: dict[str, Any], baseline: dict[str, Any]) -> None:
    comparison = {}
    for name, path in CORE_METRICS.items():
        current = nested(report, path)
        previous = nested(baseline, path)
        if current is None or previous is None:
            continue
        delta = current - previous
        comparison[name] = {
            "baseline": previous,
            "current": current,
            "delta": delta,
            "delta_percent": round((delta / previous) * 100, 1) if previous else None,
        }
    report["comparison"] = comparison


def evaluate_gates(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    checks = [
        ("run_ms", nested(report, CORE_METRICS["run_ms"]), args.max_run_ms),
        (
            "detection_to_notification_ms",
            nested(report, CORE_METRICS["detection_to_notification_ms"]),
            args.max_detection_to_notification_ms,
        ),
        ("codex_ms", nested(report, CORE_METRICS["codex_ms"]), args.max_codex_ms),
        ("tool_calls", nested(report, CORE_METRICS["tool_calls"]), args.max_tool_calls),
    ]
    failures = []
    for name, actual, limit in checks:
        if limit is None:
            continue
        if actual is None:
            failures.append(f"{name} is unavailable")
        elif actual > limit:
            failures.append(f"{name}={actual} exceeds {limit}")
    if not report.get("correctness", {}).get("available"):
        failures.append("correctness validation is unavailable")
    elif not report["correctness"].get("valid"):
        failures.append("correctness validation failed")
    return failures


def atomic_write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def markdown(report: dict[str, Any]) -> str:
    timings = report["timings_ms"]
    usage = report["codex"]
    correctness_result = report["correctness"]
    lines = [
        f"# Voice Memo Benchmark {report['memo_id']}",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Outcome: `{report.get('outcome')}`",
        f"- Pipeline: `{timings.get('run')} ms`",
        f"- Detection to notification: `{timings.get('detection_to_notification')} ms`",
        f"- Codex: `{timings.get('codex')} ms`, `{usage.get('total_tokens')}` tokens, `{usage.get('tool_calls')}` tool calls",
        f"- Correctness: `{'pass' if correctness_result.get('valid') else 'fail'}`",
    ]
    if report.get("gate_failures"):
        lines.append(f"- Gates: `fail` ({'; '.join(report['gate_failures'])})")
    else:
        lines.append("- Gates: `pass`")
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--branch", help="target branch; defaults to the local automation config")
    result.add_argument("--run-id")
    result.add_argument("--memo-id", type=int)
    result.add_argument("--compare", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--format", choices=["json", "markdown"], default="json")
    result.add_argument("--max-run-ms", type=int)
    result.add_argument("--max-detection-to-notification-ms", type=int)
    result.add_argument("--max-codex-ms", type=int)
    result.add_argument("--max-tool-calls", type=int)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        repo = args.repo.resolve()
        branch = configured_branch(repo, args.branch)
        report = build_report(repo, branch, args.run_id, args.memo_id)
        if args.compare:
            add_comparison(report, json.loads(args.compare.read_text(encoding="utf-8")))
        report["gate_failures"] = evaluate_gates(report, args)
        if args.output:
            atomic_write(args.output, report)
        if args.format == "markdown":
            sys.stdout.write(markdown(report))
        else:
            json.dump(report, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        return 3 if report["gate_failures"] else 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
