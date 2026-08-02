# Automation Contract

The primary trigger is `/Applications/Voice Memo Agent.app`, managed by `~/Library/LaunchAgents/com.nrgapple.VoiceMemoAgent.plist`. It watches for newly created `.m4a` files and runs `scripts/sync_voice_memos.py`.

```text
Use $sync-voice-memos-to-notes in scheduled-sync mode.
Report successful imports and actionable failures. Stay quiet when no new memo exists.
```

Retain a Codex automation with the same prompt every 12 hours as reconciliation only. The skill invokes the same CLI, so the automation itself does not orchestrate the workflow.

Each run must:

- operate only when the desktop app is running and the machine is awake;
- process no more than five memos, oldest first;
- invoke Codex zero times unless a locally transcribed memo contains an exact configured work trigger;
- invoke Codex once per qualified memo only for title generation and a structured contextual Markdown edit plan;
- silently mark recordings without a configured work trigger as ignored;
- assign each qualified recording a concise descriptive title and queue a best-effort Voice Memos rename;
- make one commit per memo and push a unique review branch by default;
- mark the memo complete only after that commit is merged into the configured target branch, without a second Codex call;
- remain silent for no-op and active-lease runs;
- report `memo id`, generated title, affected notes, and commit SHA after success;
- report permission, transcription, dirty-checkout, validation, or Git failures when user action is required.
- notify once on the first failed import attempt and once more if it reaches three consecutive failures, without exposing error details in the notification;
- log and notify on coordinator launch failures, missing or malformed results, and watcher startup failures, limiting watcher alerts to one per app lifetime;
- keep committed imports complete when Voice Memos is unavailable or locked, and retry their queued renames independently after wake, session activation, startup, or reconciliation;

Use the state file and provenance marker together for idempotency. A memo is complete only after its commit is pushed and `state.py success` records the SHA.
Rename state is independent: `rename_status: pending` never changes a committed memo back to pending import status and never suppresses its success notification.

The coordinator returns JSON with `ok`, `no_op`, `reviews`, `imports`, `actionable_failures`, `ignored_count`, and `metrics`. Notifications and callers consume this structure and never parse model prose.

Telemetry is JSONL and correlated by `run_id`. Each attempted memo emits a durable, schema-versioned `memo-metrics` event before the result file can be replaced by a later run. It may contain stage timestamps/durations, queue counts, memo IDs, transcript source/cache/character counts, retrieval counts, Codex usage, affected-file counts, commit SHAs, and provider request IDs. It must never contain command output, prompts, transcripts, note contents, audio, or credentials.
