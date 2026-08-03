#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
STATE = SCRIPTS / "state.py"
VALIDATE = SCRIPTS / "validate_diff.py"
QUALIFY = SCRIPTS / "qualify_transcript.py"
VALIDATE_TITLE = SCRIPTS / "validate_title.py"
RESOLVE_JOURNAL_DATE = SCRIPTS / "resolve_journal_date.py"
SYNC = SCRIPTS / "sync_voice_memos.py"
BENCHMARK = SCRIPTS / "benchmark.py"
AGENT = Path(
    os.environ.get(
        "VOICE_MEMO_AGENT_TEST_BINARY",
        "/Applications/Voice Memo Agent.app/Contents/MacOS/VoiceMemoAgent",
    )
)


def run(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True, env=env)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        run("python3", str(STATE), "--repo", str(self.repo), "setup")

    def tearDown(self):
        self.temp.cleanup()

    def state(self, *args: str, check: bool = True):
        return run("python3", str(STATE), "--repo", str(self.repo), *args, check=check)

    def test_baseline_pending_and_success(self):
        self.state("baseline", "--ids", "1", "2")
        pending = json.loads(self.state("pending", "--ids", "1", "2", "3", "4").stdout)
        self.assertEqual(pending, [3, 4])
        self.state("start", "--id", "3", "--title", "Idea", "--recorded-at", "2026-08-01T12:00:00Z")
        self.state("success", "--id", "3", "--commit", "abc123")
        pending = json.loads(self.state("pending", "--ids", "1", "2", "3", "4").stdout)
        self.assertEqual(pending, [4])

    def test_fresh_configuration_defaults_to_review_mode_without_personal_repository(self):
        config = json.loads((self.repo / ".voice-memo-automation/config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["publish_mode"], "review")
        self.assertEqual(config["repository"], "")
        self.assertEqual(config["semantic_model"], "")
        self.assertEqual(config["configuration_version"], 4)

    def test_review_state_is_not_reprocessed_before_merge(self):
        self.state("baseline", "--ids", "1")
        self.state("start", "--id", "2", "--title", "Recording", "--recorded-at", "2026-08-01T12:00:00Z")
        reviewed = json.loads(self.state(
            "review", "--id", "2", "--branch", "voice-memo/review-2-deadbeef",
            "--commit", "deadbeef", "--title", "Review Memo Changes",
            "--affected-note", "projects/example.md",
        ).stdout)
        self.assertEqual(reviewed["status"], "awaiting_review")
        self.assertEqual(json.loads(self.state("pending", "--ids", "1", "2").stdout), [])
        pending = json.loads(self.state("review-pending").stdout)
        self.assertEqual(pending[0]["affected_notes"], ["projects/example.md"])

    def test_ignored_memo_is_not_pending(self):
        self.state("baseline", "--ids", "1")
        self.state("start", "--id", "2", "--title", "Personal", "--recorded-at", "2026-08-01T12:00:00Z")
        ignored = json.loads(self.state("ignore", "--id", "2", "--reason", "missing work trigger").stdout)
        self.assertEqual(ignored["status"], "ignored")
        pending = json.loads(self.state("pending", "--ids", "1", "2", "3").stdout)
        self.assertEqual(pending, [3])

    def test_rename_queue_is_independent_from_import_status(self):
        self.state("baseline", "--ids", "1")
        self.state(
            "start", "--id", "2", "--title", "Recording 10",
            "--recorded-at", "2026-08-01T12:00:00Z", "--duration", "11.4",
        )
        queued = json.loads(
            self.state(
                "rename-queue",
                "--id",
                "2",
                "--original-title",
                "Recording 10",
                "--title",
                "Quarterly Planning Decisions",
            ).stdout
        )
        self.assertEqual(queued["rename_status"], "pending")
        self.assertEqual(json.loads(self.state("rename-pending").stdout), [])

        self.state("success", "--id", "2", "--commit", "abc123")
        failed = json.loads(
            self.state("rename-fail", "--id", "2", "--message", "Voice Memos is locked").stdout
        )
        self.assertEqual(failed["record"]["status"], "committed")
        self.assertEqual(failed["record"]["rename_status"], "pending")
        self.assertFalse(failed["actionable"])
        pending = json.loads(self.state("rename-pending").stdout)
        self.assertEqual(pending[0]["id"], 2)
        self.assertEqual(pending[0]["target_title"], "Quarterly Planning Decisions")
        self.assertEqual(pending[0]["recorded_at"], "2026-08-01T12:00:00Z")
        self.assertEqual(pending[0]["duration"], 11.4)

        renamed = json.loads(
            self.state(
                "renamed",
                "--id",
                "2",
                "--original-title",
                "Recording 10",
                "--title",
                "Quarterly Planning Decisions",
            ).stdout
        )
        self.assertEqual(renamed["original_title"], "Recording 10")
        self.assertEqual(renamed["renamed_title"], "Quarterly Planning Decisions")
        self.assertEqual(renamed["status"], "committed")
        self.assertEqual(renamed["rename_status"], "renamed")
        self.assertEqual(json.loads(self.state("rename-pending").stdout), [])

    def test_rename_failure_threshold_is_separate(self):
        self.state("baseline", "--ids", "1")
        self.state("start", "--id", "2", "--title", "Recording 10", "--recorded-at", "2026-08-01T12:00:00Z")
        self.state(
            "rename-queue",
            "--id",
            "2",
            "--original-title",
            "Recording 10",
            "--title",
            "Quarterly Planning Decisions",
        )
        self.state("success", "--id", "2", "--commit", "abc123")
        for expected in range(1, 4):
            result = json.loads(
                self.state("rename-fail", "--id", "2", "--message", "Voice Memos unavailable").stdout
            )
            self.assertEqual(result["record"]["rename_consecutive_failures"], expected)
        self.assertTrue(result["actionable"])
        self.assertEqual(result["record"]["status"], "committed")

    def test_lease_and_failures(self):
        self.state("acquire", "--owner", "one")
        busy = self.state("acquire", "--owner", "two", check=False)
        self.assertEqual(busy.returncode, 2)
        self.state("release", "--owner", "one")
        self.state("start", "--id", "9", "--title", "Memo", "--recorded-at", "now")
        actionable = []
        for expected in range(1, 4):
            result = json.loads(self.state("fail", "--id", "9", "--stage", "transcription", "--message", "no audio").stdout)
            self.assertEqual(result["record"]["consecutive_failures"], expected)
            actionable.append(result["actionable"])
        self.assertEqual(actionable, [True, False, True])
        self.assertTrue(result["actionable"])
        recovered = json.loads(self.state("success", "--id", "9", "--commit", "def456").stdout)
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertNotIn("last_error", recovered)

    def test_prune(self):
        transcript = self.repo / ".voice-memo-automation/transcripts/1.txt"
        transcript.write_text("old", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(transcript, (old, old))
        result = json.loads(self.state("prune", "--days", "30").stdout)
        self.assertEqual(result["removed"], ["1.txt"])


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        run("git", "init", "-q", "-b", "master", str(self.repo))
        run("git", "-C", str(self.repo), "config", "user.email", "test@example.com")
        run("git", "-C", str(self.repo), "config", "user.name", "Test")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_new_note(self):
        note = self.repo / "journal/2026-08-01-saturday.md"
        note.parent.mkdir()
        note.write_text("# Journal\n\nIdea.\n<!-- voice-memo-id:7 -->\n", encoding="utf-8")
        run("git", "-C", str(self.repo), "add", str(note))
        result = run("python3", str(VALIDATE), "--repo", str(self.repo), "--memo-id", "7")
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_rejects_deletions(self):
        note = self.repo / "note.md"
        note.write_text("one\ntwo\n", encoding="utf-8")
        run("git", "-C", str(self.repo), "add", "note.md")
        run("git", "-C", str(self.repo), "commit", "-qm", "base")
        note.write_text("one\n<!-- voice-memo-id:8 -->\n", encoding="utf-8")
        run("git", "-C", str(self.repo), "add", "note.md")
        result = run("python3", str(VALIDATE), "--repo", str(self.repo), "--memo-id", "8", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deleted lines", result.stdout)

    def test_rejects_non_markdown(self):
        path = self.repo / "audio.m4a"
        path.write_bytes(b"audio")
        run("git", "-C", str(self.repo), "add", "audio.m4a")
        result = run("python3", str(VALIDATE), "--repo", str(self.repo), "--memo-id", "9", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-Markdown", result.stdout)


class QualificationTests(unittest.TestCase):
    def test_accepts_configured_work_phrases(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "memo.txt"
            transcript.write_text("Okay, this is a Work-note about the release.", encoding="utf-8")
            result = run("python3", str(QUALIFY), "--transcript", str(transcript))
            self.assertEqual(json.loads(result.stdout)["matched_phrase"], "work note")

    def test_rejects_personal_memo(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "memo.txt"
            transcript.write_text("Remember to buy groceries on the way home.", encoding="utf-8")
            result = run("python3", str(QUALIFY), "--transcript", str(transcript), check=False)
            self.assertEqual(result.returncode, 3)
            self.assertFalse(json.loads(result.stdout)["eligible"])

    def test_does_not_match_for_workout_as_for_work(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "memo.txt"
            transcript.write_text("I made a plan for workouts this weekend.", encoding="utf-8")
            result = run("python3", str(QUALIFY), "--transcript", str(transcript), check=False)
            self.assertEqual(result.returncode, 3)
            self.assertFalse(json.loads(result.stdout)["eligible"])


class TitleTests(unittest.TestCase):
    def test_accepts_concise_descriptive_title(self):
        result = run(
            "python3",
            str(VALIDATE_TITLE),
            "--title",
            "Quarterly Planning Decisions",
        )
        self.assertEqual(json.loads(result.stdout)["title"], "Quarterly Planning Decisions")

    def test_rejects_routing_phrase(self):
        result = run(
            "python3",
            str(VALIDATE_TITLE),
            "--title",
            "Work Note Quarterly Planning",
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("routing phrase", result.stdout)

    def test_rejects_overlong_title(self):
        result = run(
            "python3",
            str(VALIDATE_TITLE),
            "--title",
            "One Two Three Four Five Six Seven Eight Nine",
            check=False,
        )
        self.assertEqual(result.returncode, 3)


class JournalDateTests(unittest.TestCase):
    def resolve(self, recorded_at: str):
        result = run(
            "python3",
            str(RESOLVE_JOURNAL_DATE),
            "--recorded-at",
            recorded_at,
        )
        return json.loads(result.stdout)

    def test_weekday_keeps_recording_date(self):
        result = self.resolve("2026-07-31T17:30:00-04:00")
        self.assertEqual(result["journal_date"], "2026-07-31")
        self.assertFalse(result["shifted_to_monday"])

    def test_saturday_routes_to_following_monday(self):
        result = self.resolve("2026-08-01T08:15:00-04:00")
        self.assertEqual(result["journal_date"], "2026-08-03")
        self.assertTrue(result["shifted_to_monday"])

    def test_sunday_routes_to_following_monday(self):
        result = self.resolve("2026-08-02T23:45:00-04:00")
        self.assertEqual(result["journal_date"], "2026-08-03")
        self.assertTrue(result["shifted_to_monday"])

    def test_date_only_input_is_supported(self):
        result = self.resolve("2026-08-03")
        self.assertEqual(result["journal_date"], "2026-08-03")
        self.assertFalse(result["shifted_to_monday"])


class SyncCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "notes"
        self.voice_cli = self.root / "voice.mjs"
        self.codex = self.root / "fake-codex.py"
        self.codex_calls = self.root / "codex-calls"
        run("git", "init", "-q", "--bare", "--initial-branch=master", str(self.remote))
        run("git", "clone", "-q", str(self.remote), str(self.repo))
        run("git", "-C", str(self.repo), "config", "user.email", "test@example.com")
        run("git", "-C", str(self.repo), "config", "user.name", "Test")
        journal = self.repo / "journal/2026-08-03-Monday.md"
        journal.parent.mkdir(parents=True)
        journal.write_text("# 2026-08-03-Monday\n", encoding="utf-8")
        project = self.repo / "projects/purchaser-sandbox.md"
        project.parent.mkdir(parents=True)
        project.write_text("# Purchaser Sandbox\n\nTracks sandbox purchaser accounts.\n", encoding="utf-8")
        excluded = self.repo / "meetings/transcripts/purchaser-secret.md"
        excluded.parent.mkdir(parents=True)
        excluded.write_text("# Must Never Be Retrieved\npurchaser sandbox secret\n", encoding="utf-8")
        run("git", "-C", str(self.repo), "add", ".")
        run("git", "-C", str(self.repo), "commit", "-qm", "Initial notes")
        run("git", "-C", str(self.repo), "push", "-q", "origin", "master")
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.voice-memo-automation/\n", encoding="utf-8")
        run("python3", str(STATE), "--repo", str(self.repo), "setup")
        config_path = self.repo / ".voice-memo-automation/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["publish_mode"] = "direct"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        run("python3", str(STATE), "--repo", str(self.repo), "baseline", "--ids", "1")
        self.write_voice("personal reminder")
        self.write_codex()

    def tearDown(self):
        self.temp.cleanup()

    def write_voice(self, transcript: str, include_new: bool = True):
        memos = [{
            "id": 1,
            "title": "Baseline",
            "date": "2026-07-31T09:00:00-04:00",
            "duration": 8.0,
            "path": "baseline.m4a",
        }]
        if include_new:
            memos.append({
                "id": 2,
                "title": "Recording 2",
                "date": "2026-08-01T09:00:00-04:00",
                "duration": 11.0,
                "path": "new.m4a",
            })
        source = f"""#!/usr/bin/env node
const memos = {json.dumps(memos)};
const transcript = {json.dumps(transcript)};
const command = process.argv[2];
const idIndex = process.argv.indexOf('--id');
const id = idIndex >= 0 ? Number(process.argv[idIndex + 1]) : null;
if (command === 'list') console.log(JSON.stringify({{memos, total: memos.length}}));
else if (command === 'get') console.log(JSON.stringify(memos.find(memo => memo.id === id)));
else if (command === 'transcript') console.log(JSON.stringify({{id, source: 'fixture', text: transcript}}));
else process.exit(2);
"""
        self.voice_cli.write_text(source, encoding="utf-8")

    def write_codex(
        self,
        push_conflict: bool = False,
        confidence: str = "high",
        target_path: str = "projects/purchaser-sandbox.md",
        title: str = "Purchaser Sandbox Account Review",
        content: str = "## Purchaser review\n- Review sandbox purchaser accounts.\n<!-- voice-memo-id:2 -->",
        mode: str = "append",
        delay_seconds: float = 0,
    ):
        conflict = ""
        if push_conflict:
            conflict = f"""
conflict = Path({str(self.root / 'conflict')!r})
subprocess.run(['git', 'clone', '-q', {str(self.remote)!r}, str(conflict)], check=True)
subprocess.run(['git', '-C', str(conflict), 'config', 'user.email', 'other@example.com'], check=True)
subprocess.run(['git', '-C', str(conflict), 'config', 'user.name', 'Other'], check=True)
(conflict / 'remote.md').write_text('# Remote update\\n', encoding='utf-8')
subprocess.run(['git', '-C', str(conflict), 'add', 'remote.md'], check=True)
subprocess.run(['git', '-C', str(conflict), 'commit', '-qm', 'Concurrent update'], check=True)
subprocess.run(['git', '-C', str(conflict), 'push', '-q', 'origin', 'master'], check=True)
"""
        source = f"""#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv
output = Path(args[args.index('-o') + 1])
Path({str(self.codex_calls)!r}).write_text('called', encoding='utf-8')
prompt = sys.stdin.read()
Path({str(self.root / 'semantic-prompt.txt')!r}).write_text(prompt, encoding='utf-8')
time.sleep({delay_seconds!r})
{conflict}
output.write_text(json.dumps({{
    'title': {title!r},
    'summary': 'Added the account review task.',
    'confidence': {confidence!r},
    'placement_reason': 'The existing purchaser project is directly relevant.',
    'edits': [{{
        'path': {target_path!r},
        'mode': {mode!r},
        'content': {content!r},
    }}],
}}), encoding='utf-8')
print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': 900, 'output_tokens': 100, 'total_tokens': 0}}}}))
"""
        self.codex.write_text(source, encoding="utf-8")
        self.codex.chmod(0o755)

    def sync(self, check: bool = True, extra_args: tuple[str, ...] = ()):
        node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").is_file() else (shutil.which("node") or "node")
        result = run(
            "python3",
            str(SYNC),
            "--repo",
            str(self.repo),
            "--codex-path",
            str(self.codex),
            "--node-path",
            node,
            "--voice-memo-cli",
            str(self.voice_cli),
            "--rename-cli",
            "/usr/bin/false",
            *extra_args,
            check=False,
        )
        if check and result.returncode and not result.stdout.strip():
            self.fail(result.stderr)
        return result

    def record(self, memo_id: int):
        return json.loads(run("python3", str(STATE), "--repo", str(self.repo), "show", "--id", str(memo_id)).stdout)

    def test_unqualified_memo_never_calls_codex(self):
        self.write_voice("This is a plan for workouts and groceries.")
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["metrics"]["codex_calls"], 0)
        self.assertFalse(self.codex_calls.exists())
        self.assertEqual(self.record(2)["status"], "ignored")

    def test_qualified_memo_commits_once_and_rename_failure_is_nonblocking(self):
        self.write_voice("Work note: review purchaser accounts in the sandbox.")
        first = json.loads(self.sync().stdout)
        self.assertTrue(first["ok"])
        self.assertEqual(first["metrics"]["codex_calls"], 1)
        self.assertEqual(first["metrics"]["memos"][0]["codex"]["total_tokens"], 1000)
        self.assertIn("T", first["metrics"]["memos"][0]["recording_ended_at"])
        self.assertGreaterEqual(first["metrics"]["memos"][0]["retrieval"]["candidate_files"], 2)
        self.assertEqual(first["imports"][0]["rename_status"], "pending")
        self.assertEqual(self.record(2)["status"], "committed")
        note = self.repo / "projects/purchaser-sandbox.md"
        self.assertEqual(note.read_text(encoding="utf-8").count("voice-memo-id:2"), 1)
        sync_log = self.repo / ".voice-memo-automation/sync.log"
        self.assertNotIn("Review sandbox purchaser accounts", sync_log.read_text(encoding="utf-8"))
        prompt = (self.root / "semantic-prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("purchaser-secret.md", prompt)
        self.assertNotIn("Must Never Be Retrieved", prompt)
        self.assertIn("only explicitly stated actionable tasks", prompt)
        self.assertIn("do not invent actions such as confirming", prompt)
        commit_count = int(run("git", "--git-dir", str(self.remote), "rev-list", "--count", "master").stdout)

        second = json.loads(self.sync().stdout)
        self.assertEqual(second["metrics"]["codex_calls"], 0)
        self.assertEqual(int(run("git", "--git-dir", str(self.remote), "rev-list", "--count", "master").stdout), commit_count)
        self.assertEqual(note.read_text(encoding="utf-8").count("voice-memo-id:2"), 1)

        durable = [
            json.loads(line) for line in sync_log.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "memo-metrics"
        ]
        self.assertEqual(durable[-1]["metrics"]["codex"]["total_tokens"], 1000)
        self.assertEqual(durable[-1]["metrics"]["outcome"], "imported")
        events = [json.loads(line) for line in sync_log.read_text(encoding="utf-8").splitlines()]
        for event in events:
            if event.get("run_id") == first["run_id"] and event.get("event") == "memo-metrics":
                event["metrics"]["codex"]["total_tokens"] = 0
        sync_log.write_text(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
            encoding="utf-8",
        )
        benchmark = json.loads(run(
            "python3", str(BENCHMARK), "--repo", str(self.repo),
            "--run-id", first["run_id"], "--max-tool-calls", "0",
        ).stdout)
        self.assertEqual(benchmark["codex"]["total_tokens"], 1000)
        self.assertTrue(benchmark["correctness"]["valid"])
        self.assertEqual(benchmark["gate_failures"], [])
        baseline = self.root / "baseline.json"
        run(
            "python3", str(BENCHMARK), "--repo", str(self.repo),
            "--run-id", first["run_id"], "--output", str(baseline),
        )
        compared = json.loads(run(
            "python3", str(BENCHMARK), "--repo", str(self.repo),
            "--run-id", first["run_id"], "--compare", str(baseline),
        ).stdout)
        self.assertEqual(compared["comparison"]["codex_ms"]["delta"], 0)
        failed_gate = run(
            "python3", str(BENCHMARK), "--repo", str(self.repo),
            "--run-id", first["run_id"], "--max-codex-ms", "1", check=False,
        )
        self.assertEqual(failed_gate.returncode, 3)

    def test_review_mode_waits_for_merge_then_recovers_without_second_codex_call(self):
        config_path = self.repo / ".voice-memo-automation/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["publish_mode"] = "review"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.write_voice("Work note: review purchaser accounts in the sandbox.")

        first = json.loads(self.sync().stdout)
        self.assertEqual(first["imports"], [])
        self.assertEqual(len(first["reviews"]), 1)
        review = first["reviews"][0]
        self.assertEqual(self.record(2)["status"], "awaiting_review")
        master_note = run(
            "git", "--git-dir", str(self.remote), "show", "master:projects/purchaser-sandbox.md"
        ).stdout
        self.assertNotIn("voice-memo-id:2", master_note)
        branch_note = run(
            "git", "--git-dir", str(self.remote), "show",
            f"{review['branch']}:projects/purchaser-sandbox.md",
        ).stdout
        self.assertIn("voice-memo-id:2", branch_note)

        self.codex_calls.unlink(missing_ok=True)
        waiting = json.loads(self.sync().stdout)
        self.assertEqual(waiting["metrics"]["codex_calls"], 0)
        self.assertFalse(self.codex_calls.exists())
        self.assertEqual(self.record(2)["status"], "awaiting_review")

        reviewer = self.root / "reviewer"
        run("git", "clone", "-q", str(self.remote), str(reviewer))
        run("git", "-C", str(reviewer), "config", "user.email", "reviewer@example.com")
        run("git", "-C", str(reviewer), "config", "user.name", "Reviewer")
        run("git", "-C", str(reviewer), "merge", "--no-ff", "-m", "Approve voice memo", f"origin/{review['branch']}")
        run("git", "-C", str(reviewer), "push", "-q", "origin", "master")

        recovered = json.loads(self.sync().stdout)
        self.assertEqual(recovered["metrics"]["codex_calls"], 0)
        self.assertEqual(len(recovered["imports"]), 1)
        self.assertEqual(self.record(2)["status"], "committed")
        self.assertFalse(self.codex_calls.exists())
        benchmark = json.loads(run(
            "python3", str(BENCHMARK), "--repo", str(self.repo),
            "--run-id", recovered["run_id"],
        ).stdout)
        self.assertTrue(benchmark["correctness"]["valid"])
        self.assertEqual(benchmark["correctness"]["remote_branch"], "origin/master")

    def test_semantic_timeout_is_actionable_and_retains_retry_state(self):
        config_path = self.repo / ".voice-memo-automation/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["semantic_timeout_seconds"] = 0.05
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.write_voice("Work note: review purchaser accounts in the sandbox.")
        self.write_codex(delay_seconds=1)

        result = json.loads(self.sync(check=False).stdout)
        self.assertEqual(result["imports"], [])
        self.assertEqual(result["actionable_failures"][0]["stage"], "semantic-edit")
        self.assertIn("timed out", result["actionable_failures"][0]["message"])
        self.assertEqual(self.record(2)["status"], "pending")

    def test_dirty_checkout_stops_before_voice_or_codex_work(self):
        (self.repo / "dirty.md").write_text("dirty\n", encoding="utf-8")
        result = json.loads(self.sync(check=False).stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actionable_failures"][0]["stage"], "git-preflight")
        self.assertFalse(self.codex_calls.exists())

    def test_missing_checkout_fails_without_creating_automation_state(self):
        missing = self.root / "missing-notes"
        node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").is_file() else (shutil.which("node") or "node")
        completed = run(
            "python3",
            str(SYNC),
            "--repo",
            str(missing),
            "--node-path",
            node,
            check=False,
        )
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actionable_failures"][0]["stage"], "git-preflight")
        self.assertIn("is not a Git repository", result["actionable_failures"][0]["message"])
        self.assertFalse(missing.exists())

    def test_invalid_runtime_limit_returns_structured_configuration_failure(self):
        config_path = self.repo / ".voice-memo-automation/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["command_timeout_seconds"] = 0
        config_path.write_text(json.dumps(config), encoding="utf-8")

        result = json.loads(self.sync(check=False).stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actionable_failures"][0]["stage"], "configuration")
        self.assertEqual(result["metrics"]["codex_calls"], 0)

    def test_active_lease_is_a_silent_noop(self):
        run("python3", str(STATE), "--repo", str(self.repo), "acquire", "--owner", "other")
        result = json.loads(self.sync().stdout)
        self.assertTrue(result["no_op"])
        self.assertEqual(result["metrics"]["codex_calls"], 0)
        self.assertFalse(self.codex_calls.exists())

    def test_event_run_waits_for_file_readiness_and_defers_old_renames(self):
        self.write_voice("Personal reminder about groceries.")
        recording = self.root / "new.m4a"
        recording.write_bytes(b"fixture audio")
        config_path = self.repo / ".voice-memo-automation/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["readiness_stable_checks"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = json.loads(self.sync(extra_args=(
            "--recording-file", str(recording),
            "--detected-at", "2026-08-01T13:00:15Z",
            "--run-id", "fixture-event-run",
        )).stdout)
        self.assertEqual(result["run_id"], "fixture-event-run")
        self.assertTrue(result["metrics"]["queue"]["rename_retry_deferred"])
        self.assertGreaterEqual(result["metrics"]["stages_ms"]["readiness_and_listing"], 900)

    def test_event_run_emits_only_fixed_privacy_safe_demo_progress(self):
        self.write_voice("Work note: review purchaser accounts in the sandbox.")
        recording = self.root / "new.m4a"
        recording.write_bytes(b"fixture audio")
        result_file = self.root / "result.json"
        completed = self.sync(extra_args=(
            "--recording-file", str(recording),
            "--detected-at", "2026-08-01T13:00:15Z",
            "--run-id", "fixture-demo-run",
            "--result-file", str(result_file),
            "--demo-progress",
        ))
        result = json.loads(result_file.read_text(encoding="utf-8"))
        progress = completed.stdout.splitlines()

        self.assertTrue(result["ok"])
        self.assertEqual(progress, [
            "voice-memo-demo:listening",
            "voice-memo-demo:qualified",
            "voice-memo-demo:organizing",
            "voice-memo-demo:drafting",
            "voice-memo-demo:validated",
            "voice-memo-demo:imported",
        ])
        self.assertNotIn("purchaser", completed.stdout.casefold())
        self.assertNotIn("fixture-demo-run", completed.stdout)

    def test_push_conflict_leaves_memo_pending_without_marker_on_remote(self):
        self.write_voice("For work, review purchaser accounts in the sandbox.")
        self.write_codex(push_conflict=True)
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["imports"], [])
        self.assertEqual(result["metrics"]["codex_calls"], 1)
        self.assertEqual(self.record(2)["status"], "pending")
        remote_note = run("git", "--git-dir", str(self.remote), "show", "master:projects/purchaser-sandbox.md").stdout
        self.assertNotIn("voice-memo-id:2", remote_note)

    def test_specific_person_note_beats_generic_calendar_and_common_word_matches(self):
        target = self.repo / "FY25_Q2_Geiger_SWE3.md"
        target.write_text("# FY25 Q2 SWE3 Review\n\nPrior performance notes.\n", encoding="utf-8")
        for index in range(8):
            distractor = self.repo / f"journal/2025-0{index + 1}-01-Wednesday.md"
            distractor.write_text(
                "# Wednesday\n\nThe review is for the weekly interview process.\n",
                encoding="utf-8",
            )
        run("git", "-C", str(self.repo), "add", ".")
        run("git", "-C", str(self.repo), "commit", "-qm", "Add retrieval fixtures")
        run("git", "-C", str(self.repo), "push", "-q", "origin", "master")

        self.write_voice("Work note, review the interview with Matt Geiger for Wednesday.")
        self.write_codex(
            target_path="FY25_Q2_Geiger_SWE3.md",
            title="Matt Geiger Interview Review",
            content="## Interview review\n- Review the interview with Matt Geiger.\n<!-- voice-memo-id:2 -->",
        )
        result = json.loads(self.sync().stdout)

        self.assertEqual(result["actionable_failures"], [])
        self.assertEqual(result["imports"][0]["affected_notes"], ["FY25_Q2_Geiger_SWE3.md"])
        prompt = (self.root / "semantic-prompt.txt").read_text(encoding="utf-8")
        candidate_json = prompt.split("Candidate note excerpts:\n", 1)[1].split(
            "\n\nCandidate graph context", 1
        )[0]
        candidates = json.loads(candidate_json)
        self.assertIn("FY25_Q2_Geiger_SWE3.md", [item["path"] for item in candidates])

    def test_new_durable_note_must_connect_to_existing_foam_graph(self):
        self.write_voice("Work note: record the purchaser sandbox review decision.")
        self.write_codex(
            mode="create",
            target_path="2026-08-01-purchaser-sandbox-review.md",
            title="Purchaser Sandbox Review Decision",
            content=(
                "# Purchaser Sandbox Review Decision\n\n"
                "Related project: [[projects/purchaser-sandbox]]\n\n"
                "- Review sandbox purchaser accounts.\n"
                "<!-- voice-memo-id:2 -->"
            ),
        )
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["actionable_failures"], [])
        self.assertEqual(result["imports"][0]["affected_notes"], ["2026-08-01-purchaser-sandbox-review.md"])

    def test_new_durable_note_without_graph_connection_is_rejected(self):
        self.write_voice("Work note: record the purchaser sandbox review decision.")
        self.write_codex(
            mode="create",
            target_path="2026-08-01-purchaser-sandbox-review.md",
            title="Purchaser Sandbox Review Decision",
            content=(
                "# Purchaser Sandbox Review Decision\n\n"
                "- Review sandbox purchaser accounts.\n"
                "<!-- voice-memo-id:2 -->"
            ),
        )
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["imports"], [])
        self.assertEqual(result["actionable_failures"][0]["stage"], "validation")
        self.assertIn("would be orphaned", self.record(2)["last_error"])

    def test_unresolved_foam_wikilink_is_rejected(self):
        self.write_voice("Work note: review purchaser accounts in the sandbox.")
        self.write_codex(
            content=(
                "## Purchaser review\n"
                "- Review [[missing-project]] accounts.\n"
                "<!-- voice-memo-id:2 -->"
            ),
        )
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["imports"], [])
        self.assertIn("unresolved or ambiguous Foam wikilink", self.record(2)["last_error"])

    def test_symlink_note_cannot_escape_the_worktree(self):
        external = self.root / "outside.md"
        external.write_text("# Outside\n\nMust remain unchanged.\n", encoding="utf-8")
        linked = self.repo / "projects/linked.md"
        linked.symlink_to(external)
        run("git", "-C", str(self.repo), "add", "projects/linked.md")
        run("git", "-C", str(self.repo), "commit", "-qm", "Add hostile symlink fixture")
        run("git", "-C", str(self.repo), "push", "-q", "origin", "master")
        self.write_voice("Work note: update the linked purchaser project.")
        self.write_codex(target_path="projects/linked.md")

        result = json.loads(self.sync().stdout)
        self.assertEqual(result["imports"], [])
        self.assertEqual(self.record(2)["last_failure_stage"], "validation")
        self.assertEqual(external.read_text(encoding="utf-8"), "# Outside\n\nMust remain unchanged.\n")

    def test_blocked_roots_are_case_insensitive(self):
        blocked = self.repo / "Attachments/purchaser-private.md"
        blocked.parent.mkdir(parents=True)
        blocked.write_text("# Private attachment\n\npurchaser private phrase\n", encoding="utf-8")
        run("git", "-C", str(self.repo), "add", ".")
        run("git", "-C", str(self.repo), "commit", "-qm", "Add blocked-root fixture")
        run("git", "-C", str(self.repo), "push", "-q", "origin", "master")
        self.write_voice("Work note: review the purchaser private phrase.")

        result = json.loads(self.sync().stdout)
        self.assertEqual(result["actionable_failures"], [])
        prompt = (self.root / "semantic-prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("purchaser-private.md", prompt)
        self.assertNotIn("Private attachment", prompt)

    def test_low_confidence_plan_cannot_modify_a_project_note(self):
        self.write_voice("Work note: review purchaser accounts in the sandbox.")
        self.write_codex(confidence="low")
        result = json.loads(self.sync().stdout)
        self.assertEqual(result["imports"], [])
        self.assertEqual(self.record(2)["last_failure_stage"], "validation")
        remote_note = run("git", "--git-dir", str(self.remote), "show", "master:projects/purchaser-sandbox.md").stdout
        self.assertNotIn("voice-memo-id:2", remote_note)


@unittest.skipUnless(AGENT.is_file(), "Voice Memo Agent is not installed")
class AgentTests(unittest.TestCase):
    def classify(self, path: str, *flags: str):
        return json.loads(run(str(AGENT), "classify-event", "--path", path, *flags).stdout)

    def test_created_audio_triggers(self):
        self.assertTrue(self.classify("/tmp/new.m4a", "--created", "--is-file")["should_trigger"])

    def test_modified_audio_does_not_trigger(self):
        self.assertFalse(self.classify("/tmp/existing.m4a", "--is-file")["should_trigger"])

    def test_non_audio_file_does_not_trigger(self):
        self.assertFalse(self.classify("/tmp/new.waveform", "--created", "--is-file")["should_trigger"])

    def test_demo_log_is_conversational_and_paced(self):
        with tempfile.TemporaryDirectory() as directory:
            demo_log = Path(directory) / "agent-demo.log"
            started = time.monotonic()
            result = json.loads(run(
                str(AGENT),
                "demo-log-preview",
                "--demo-log-path", str(demo_log),
                "--demo-log-interval-ms", "40",
            ).stdout)
            elapsed = time.monotonic() - started
            lines = demo_log.read_text(encoding="utf-8").splitlines()

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(elapsed, 0.20)
        self.assertEqual(len(lines), 7)
        self.assertIn("🎙️ New memo detected", lines[0])
        self.assertIn("audio locally before touching", lines[0])
        self.assertIn("📝 Transcribing on this Mac", lines[1])
        self.assertIn("recording stays local", lines[1])
        self.assertIn("💡 Found the “work note” cue", lines[2])
        self.assertIn("opt-in", lines[2])
        self.assertIn("🔎 Comparing the memo", lines[3])
        self.assertIn("best destination", lines[3])
        self.assertIn("🧠 Relevant context found", lines[4])
        self.assertIn("raw transcript", lines[4])
        self.assertIn("🛡️ Safety checks passed", lines[5])
        self.assertIn("no overwritten", lines[5])
        self.assertIn("✅ Update committed and pushed", lines[6])
        self.assertIn("notes are current", lines[6])
        self.assertTrue(all(not line.startswith("[") for line in lines))
        self.assertTrue(all(not line.lstrip().startswith("{") for line in lines))

    def test_rename_row_score_uses_recording_date_time_and_duration(self):
        result = json.loads(run(
            str(AGENT), "rename-row-score",
            "--description", "Recording 9, August 1, 2:13 PM, 0:11",
            "--recorded-at", "2026-08-01T14:13:25-04:00",
            "--duration", "11.1",
        ).stdout)
        self.assertGreaterEqual(result["score"], 10)

    def test_secure_pushover_credentials_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "pushover.json"
            credentials.write_text(
                json.dumps({"api_token": "a" * 30, "user_key": "u" * 30}),
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            environment = os.environ.copy()
            environment["VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE"] = str(credentials)
            result = json.loads(run(str(AGENT), "pushover-status", env=environment).stdout)
            self.assertTrue(result["configured"])

    def test_permissive_pushover_credentials_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "pushover.json"
            credentials.write_text(
                json.dumps({"api_token": "a" * 30, "user_key": "u" * 30}),
                encoding="utf-8",
            )
            credentials.chmod(0o644)
            environment = os.environ.copy()
            environment["VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE"] = str(credentials)
            result = run(str(AGENT), "pushover-status", env=environment, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mode 0600", result.stderr)

    def test_structured_success_creates_private_notification_payload(self):
        workflow = json.dumps({
            "ok": True,
            "no_op": False,
            "imports": [{
                "memo_id": 23,
                "title": "New Memo Detection Test",
                "affected_notes": ["journal/2026-08-03-Monday.md"],
                "commit_sha": "85822ec804d5ba962d80c8f22ac93b16ac8919a1",
                "github_url": "https://github.com/example/private-notes/commit/85822ec804d5ba962d80c8f22ac93b16ac8919a1",
                "rename_status": "pending",
            }],
            "actionable_failures": [],
            "ignored_count": 0,
            "metrics": {"codex_calls": 1, "duration_ms": 1234},
        })
        result = json.loads(
            run(str(AGENT), "notification-preview", "--workflow-json", workflow).stdout
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["title"], "Voice memo imported")
        self.assertTrue(result["url"].endswith("/commit/85822ec804d5ba962d80c8f22ac93b16ac8919a1"))

    def test_processing_started_notification_is_private(self):
        result = json.loads(
            run(
                str(AGENT),
                "processing-started-notification-preview",
                "--recording-count",
                "1",
            ).stdout
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["title"], "Voice memo found")
        self.assertEqual(result["message"], "A new voice memo was found and processing has started.")
        self.assertNotIn(".m4a", result["message"])
        self.assertIsNone(result["url"])

    def test_processing_started_notification_pluralizes_batches(self):
        result = json.loads(
            run(
                str(AGENT),
                "processing-started-notification-preview",
                "--recording-count",
                "2",
            ).stdout
        )
        self.assertEqual(result["message"], "2 new voice memos were found and processing has started.")

    def test_structured_noop_does_not_notify(self):
        workflow = json.dumps({
            "ok": True,
            "no_op": True,
            "imports": [],
            "actionable_failures": [],
            "ignored_count": 0,
            "metrics": {"codex_calls": 0, "duration_ms": 45},
        })
        result = json.loads(
            run(
                str(AGENT),
                "notification-preview",
                "--workflow-json",
                workflow,
            ).stdout
        )
        self.assertFalse(result["should_notify"])

    def test_structured_review_creates_private_notification_payload(self):
        workflow = json.dumps({
            "ok": True,
            "no_op": False,
            "imports": [],
            "reviews": [{
                "memo_id": 24,
                "title": "Review Memo Delivery",
                "affected_notes": ["journal/2026-08-03-Monday.md"],
                "commit_sha": "85822ec804d5ba962d80c8f22ac93b16ac8919a1",
                "branch": "voice-memo/review-24-85822ec8",
                "review_url": "https://github.com/example/private-notes/compare/main...voice-memo/review-24-85822ec8",
            }],
            "actionable_failures": [],
            "ignored_count": 0,
            "metrics": {"codex_calls": 1, "duration_ms": 1234},
        })
        result = json.loads(
            run(str(AGENT), "notification-preview", "--workflow-json", workflow).stdout
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["title"], "Voice memo ready for review")
        self.assertIn("Memo 24", result["message"])
        self.assertIn("/compare/main...voice-memo/review-24-85822ec8", result["url"])

    def test_structured_failure_creates_private_notification_payload(self):
        workflow = json.dumps({
            "ok": False,
            "no_op": False,
            "imports": [],
            "actionable_failures": [{
                "memo_id": 30,
                "stage": "validation",
                "message": "private note path and command output must not leave this Mac",
            }],
            "ignored_count": 0,
            "metrics": {"codex_calls": 1, "duration_ms": 1234},
        })
        result = json.loads(
            run(str(AGENT), "notification-preview", "--workflow-json", workflow).stdout
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["title"], "Voice memo import failed")
        self.assertIn("Memo 30", result["message"])
        self.assertIn("validation", result["message"])
        self.assertNotIn("private note path", result["message"])
        self.assertIsNone(result["url"])

    def test_runtime_failure_creates_generic_private_notification_payload(self):
        workflow = json.dumps({
            "ok": False,
            "no_op": False,
            "imports": [],
            "actionable_failures": [{
                "memo_id": None,
                "stage": "runtime",
                "message": "private command output must not leave this Mac",
            }],
            "ignored_count": 0,
            "metrics": {"codex_calls": 0, "duration_ms": 10},
        })
        result = json.loads(
            run(str(AGENT), "notification-preview", "--workflow-json", workflow).stdout
        )
        self.assertTrue(result["should_notify"])
        self.assertIn("Voice Memo Agent", result["message"])
        self.assertIn("runtime", result["message"])
        self.assertNotIn("private command output", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
