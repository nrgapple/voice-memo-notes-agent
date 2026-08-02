# Voice Memo Agent Runtime

`/Applications/Voice Memo Agent.app` is a locally signed, background-only macOS application with four modes:

- `watch`: listen for Voice Memos filesystem creation events;
- `sync`: invoke the deterministic coordinator once;
- `doctor`: report CLI and privacy-permission readiness;
- rename arguments: update a selected recording title through Voice Memos accessibility controls.

The LaunchAgent starts `watch` at login and keeps it running. The FSEvents stream uses file-level events and reacts only when a file is both newly created and has the `.m4a` extension. Modifications are ignored because title changes update the existing audio file.

Events are debounced for two seconds, then held only until the new audio file is stable and represented in the Voice Memos database. Multiple arrivals cause one coordinator run, and an arrival during a run queues one follow-up. Startup and six-hour reconciliation runs recover missed or coalesced events. Wake, screen-wake, and active-session notifications also trigger reconciliation so deferred UI renames can resume promptly after unlock. The state lease and memo IDs remain the authoritative duplicate protection.

The app invokes the absolute Python coordinator path with argument arrays, never a constructed shell command, and terminates a stalled coordinator after the configured timeout. The coordinator reads Voice Memos through the pinned library, transcribes and qualifies locally, and owns state, retries, Git, validation, and structured output. For a qualified memo only, it invokes one ephemeral `codex exec` with read-only sandboxing and approval policy `never` in a tiny temporary workspace. Codex receives the qualified transcript, Foam guidance, a capped path/heading vault map, capped excerpts from the strongest candidate notes, and capped resolved link/backlink context for those candidates. Transcript size, prompt size, context size, and subprocess duration have explicit limits. It returns a structured edit plan and does not own filesystem mutation, Voice Memos, state, commits, pushes, or notifications. Deterministic validation rejects paths outside the checkout, symlink traversal, ambiguous or unresolved automated wikilinks, and any new non-journal note that would be orphaned from the existing Foam graph.

When the coordinator returns a structured review or successful import, the app builds a minimal Pushover payload from the memo ID, generated title, affected note paths, commit SHA, and GitHub review/commit URL. Fresh installations push review branches; reconciliation records success after merge without another model call. For a structured actionable failure, including a nonzero coordinator exit, the app sends a separate privacy-safe alert containing only the memo ID and failure stage; raw errors, transcripts, note paths, and command output are excluded. Coordinator launch failures, timeouts, missing or malformed structured results, and watcher startup failures use the same generic runtime alert path. A watcher startup failure alerts once while the app continues its minute-by-minute recovery loop. Credentials are read from a current-user-only `0600` file by the signed app; their values are never placed in launchd or child-process environments. No notification is sent for ignored memos, clean no-op runs, or active leases. A Pushover delivery failure is logged separately and never changes memo state.

For every attempted memo, the coordinator writes a schema-versioned `memo-metrics` JSONL event before returning. The event includes outcome, recording/file/detection clocks, transcription source/cache/duration/character count, retrieval duration/candidate count/character volume, Codex duration/token usage/tool calls/confidence, validation and publish durations, affected-file count, and commit SHA. It contains no prompt, transcript, audio, note content, credentials, model prose, or command output. Because it is written to `sync.log`, later app runs may replace `agent-last-message.txt` without losing benchmark data.

The app also owns a presentation-only `agent-demo.log`. It stays silent unless FSEvents detects a newly created recording, then explains the corresponding run with timestamp-free, emoji-led sentences covering local transcription, opt-in qualification, note retrieval, contextual drafting, validation, and delivery. A private serial writer keeps messages at least 1.5 seconds apart without slowing the import worker. The demo log never includes filenames, memo IDs, run IDs, titles, transcripts, note paths, command output, or credentials. It does not replace or feed the JSONL telemetry.

Voice Memos renaming is best-effort and has independent state. The skill records the generated target title before committing and queues rename retries independently. In review mode, import success is recorded only after the commit appears on the configured target branch; direct mode records success after its remote push is verified. Recording date, time, and duration disambiguate repeated generic titles. A locked session, delayed iCloud UI row, or title-confirmation timeout leaves `rename_status` pending without changing review/import state. Later runs verify the title by memo ID before retrying, so a rename that actually succeeded despite a UI timeout is recovered without another edit.

Runtime files are local and excluded from Git:

```text
.voice-memo-automation/agent.log
.voice-memo-automation/agent.log.1
.voice-memo-automation/agent-codex.log
.voice-memo-automation/agent-demo.log
.voice-memo-automation/agent-demo.log.1
.voice-memo-automation/agent-launchd.log
.voice-memo-automation/agent-event-state.json
.voice-memo-automation/agent-last-message.txt
.voice-memo-automation/sync.log
.voice-memo-automation/sync.log.1
.voice-memo-automation/benchmarks/*.json
```

The computer must be awake for transcription and Git work. A locked session may defer accessibility-based title editing, but it does not block the note commit or Pushover notification.

Use `scripts/benchmark.py` to join coordinator metrics with watcher and notification events by `run_id`, verify the resulting Git transaction, compare a saved baseline, and enforce performance gates. Follow [`improvement-harness.md`](improvement-harness.md) for the fixture/live-canary split and product change loop.
