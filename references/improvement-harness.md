# Product Improvement Harness

Use this harness when changing event detection, transcription, retrieval, semantic planning, note application, Git delivery, notifications, or renaming. Optimize one behavior at a time and preserve both note quality and transaction safety.

## Evaluation Layers

1. **Static checks:** compile Python and Swift, validate the skill, and inspect the diff.
2. **Deterministic fixtures:** run `scripts/test_helpers.py` for qualification, weekend routing, state, dirty checkout, push conflicts, low-confidence placement, telemetry durability, benchmark gates, and notification privacy.
3. **Live health:** run `scripts/doctor.sh` against the signed app, permissions, Voice Memos database, Speech helper, GitHub, launchd, and Pushover configuration.
4. **Live canary:** record a new opted-in memo and evaluate the actual transcript, semantic placement, commit, notification, and latency. Fixture model output cannot establish note quality.

Run layers 1-2 with `scripts/run_harness.sh fixture`. Use `scripts/run_harness.sh live` to add the complete doctor pass before a canary or release.

## Benchmark CLI

Every processed memo emits a durable `memo-metrics` event in `.voice-memo-automation/sync.log`. It survives later no-op and rename-only runs and contains counts and timings, never transcript or note content.

Imports created before telemetry schema 2 do not have recoverable token totals and cannot become complete benchmark artifacts. Record one new canary after upgrading before saving the first baseline.

Inspect the latest import:

```bash
python3 scripts/benchmark.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR" \
  --format markdown
```

Select a run or memo when several recordings were batched:

```bash
python3 scripts/benchmark.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR" \
  --run-id RUN_ID \
  --memo-id MEMO_ID
```

Save a known-good local baseline. Keep it under `.voice-memo-automation/` so it cannot enter Git:

```bash
python3 scripts/benchmark.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR" \
  --output "$VOICE_MEMO_NOTES_REPO_DIR/.voice-memo-automation/benchmarks/baseline.json"
```

Compare a canary and enforce initial user-path budgets:

```bash
python3 scripts/benchmark.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR" \
  --compare "$VOICE_MEMO_NOTES_REPO_DIR/.voice-memo-automation/benchmarks/baseline.json" \
  --max-run-ms 45000 \
  --max-detection-to-notification-ms 50000 \
  --max-codex-ms 25000 \
  --max-tool-calls 0
```

Exit status is `0` on success, `2` when telemetry cannot produce a report, and `3` when a performance or correctness gate fails. The correctness gate verifies that the commit is on the configured remote target branch, the checkout is clean, one to five files changed, no lines were deleted, and exactly one memo marker was added.

## Note Quality Rubric

Review the local canary commit before accepting a semantic or retrieval change:

- the generated title is specific and accurately describes the memo;
- the destination is the best existing topic note, the authoritative journal, or a justified new note;
- Saturday and Sunday journal content goes to Monday;
- facts and decisions preserve the speaker's intent, and checkboxes appear only for explicitly stated tasks, requests, commitments, reminders, or next steps;
- the routing phrase and raw transcript are absent;
- additions are concise, fit surrounding structure, and use useful Foam links;
- no duplicate content or provenance marker exists elsewhere.

A faster run with a worse destination or lossy summary is a regression. Low-confidence content should fall back to the journal.

## Change Loop

1. Capture a known-good live baseline and record the commit SHA and run ID in the work log or pull request.
2. Make one scoped product change so latency and quality effects remain attributable.
3. Run static checks and deterministic fixtures.
4. Rebuild/sign with `scripts/bootstrap.sh`, run bootstrap a second time for idempotency, then run `scripts/doctor.sh`.
5. Record a live canary with a comparable length and say an exact work trigger.
6. Run the benchmark comparison and inspect the canary commit with the quality rubric.
7. Run the same canary class three times before accepting a noisy performance claim; compare medians for Speech, Codex, Git/network, and notification stages.
8. Publish only when correctness passes, note quality is preserved, no new actionable failure appears, and performance is within the agreed budgets.

## Interpreting Results

- Compare detection-to-notification for user-perceived speed.
- Compare run and memo durations for coordinator speed.
- Compare transcription source and cache status before attributing Speech changes.
- Compare candidate count/characters and Codex token totals before attributing semantic speed.
- Treat tool calls above zero as a semantic sandbox regression.
- Review validation and publish independently; GitHub or network variance can dominate short runs.
- Rename latency is a post-notification metric and must not be added to the import critical path.

Do not compare recording-start-to-notification across memos of different lengths. Prefer recording-end-to-notification or detection-to-notification.
