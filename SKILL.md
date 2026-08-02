---
name: sync-voice-memos-to-notes
description: Access iCloud-synced Apple Voice Memos through a deterministic local CLI and integrate opted-in recordings into a Foam Markdown notes repository. Use for bootstrap, diagnosis, manual sync, or scheduled sync of the signed Voice Memo Agent workflow.
---

# Sync Voice Memos To Notes

Use the requested mode: `bootstrap`, `doctor`, or `scheduled-sync`. The bundled coordinator owns the workflow; do not reconstruct it from prose.

## Paths

- Skill: `~/.codex/skills/sync-voice-memos-to-notes`
- Notes checkout: `$VOICE_MEMO_NOTES_REPO_DIR`, or the `--repo` value installed in the LaunchAgent
- Local state: `<notes-checkout>/.voice-memo-automation`
- MCP source: `~/.codex/tools/apple-voice-memo-mcp`
- Agent app: `/Applications/Voice Memo Agent.app`
- Agent CLI: `~/.codex/skills/sync-voice-memos-to-notes/scripts/rename_voice_memo.sh`
- Sync CLI: `~/.codex/skills/sync-voice-memos-to-notes/scripts/sync_voice_memos.py`
- LaunchAgent: `~/Library/LaunchAgents/com.nrgapple.VoiceMemoAgent.plist`
- Pushover credentials: `~/.config/voice-memo-agent/pushover.json` with mode `0600`

## Bootstrap

Read [references/setup.md](references/setup.md), run `scripts/bootstrap.sh`, then run `scripts/doctor.sh`. Report failed labeled checks exactly. Required macOS permissions must be granted by the user; never attempt to bypass TCC. The first scheduled sync baselines all existing recordings automatically without importing them.

## Doctor

Run `scripts/doctor.sh`. Use its exit status and labeled checks to distinguish runtime ABI, Voice Memos, speech helper, app signature, GitHub, repository, state, and permission failures.

## Benchmark And Improve

When measuring or changing this product, read [references/improvement-harness.md](references/improvement-harness.md). Use `scripts/run_harness.sh` for repeatable fixture/live validation, `scripts/benchmark.py` for durable live metrics and comparisons, and a real canary memo plus the note-quality rubric for semantic changes. Never claim a semantic quality improvement from mocked model output alone.

## Scheduled Sync

Run:

```bash
python3 scripts/sync_voice_memos.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR"
```

The command prints structured JSON. Stay quiet when `no_op` is true and `actionable_failures` is empty. Report each item in `reviews`, each completed item in `imports`, and any `actionable_failures`. Do not manually repeat or continue a partial transaction; retries and recovery are coordinator-owned.

Read [references/automation-contract.md](references/automation-contract.md) for guarantees and [references/note-integration.md](references/note-integration.md) only when changing semantic placement behavior.

## Event Runtime

Read [references/agent-runtime.md](references/agent-runtime.md). The signed app watches for new `.m4a` files and invokes the same sync CLI. The CLI starts `codex exec` only for a locally qualified work memo. Codex receives Foam guidance, a capped path/heading vault map, capped excerpts from the strongest candidates, and capped resolved link/backlink context in a tiny read-only workspace; deterministic Python applies and validates its structured edit plan in an isolated Git worktree. New non-journal notes must connect to the existing Foam graph, and automated wikilinks must resolve unambiguously.

Fresh installations push a per-memo review branch. The agent sends a Pushover review notification containing only the memo ID, generated title, affected note paths, commit SHA, and GitHub comparison URL. After that commit is merged into the configured target branch, reconciliation records the import without another Codex call. Notification delivery is best-effort and independent of memo state: never retry, roll back, or duplicate an import because notification delivery failed.

When an import attempt produces an actionable failure, the agent sends a privacy-safe Pushover alert containing only the memo ID and failure stage. It never includes the coordinator's error message, transcript, note path, or command output. The first failed import attempt is actionable immediately; a third consecutive failure produces one escalation without alerting on every reconciliation retry. Coordinator launch/result failures and watcher startup failures send generic runtime alerts; watcher startup alerts are limited to one per app lifetime. Rename alerts explicitly say that the import already succeeded.

## Safety

- Never delete or rename note files during an import.
- Never overwrite remote changes or force-push.
- Stop on a dirty checkout, an unpushed local commit, a push conflict, or a failed validator.
- Keep no-op runs silent. Report imported memo IDs with commit SHAs and actionable failures only.
- Rename only qualified, unseen memos. Never rename baseline or ignored recordings.
- Never let a title change block, roll back, or duplicate a successful note import or notification.
