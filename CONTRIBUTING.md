# Contributing

Thank you for improving Voice Memo Notes Agent.

## Development setup

The complete runtime requires macOS 26, Apple Voice Memos, Codex, and a dedicated private test vault. Most transaction behavior is covered by deterministic fixtures and does not access real recordings.

```bash
python3 -m pip install -r requirements-dev.txt
./scripts/run_harness.sh fixture
```

Use a focused branch and keep changes scoped. Never add recordings, transcripts, notes, credentials, state, logs, signing material, or app bundles to Git.

Changes to transcription, retrieval, model planning, note application, delivery, notifications, or renaming must follow `references/improvement-harness.md`. Semantic changes require a private live canary and human quality review; fixture output alone is insufficient.

## Pull requests

Describe the behavior changed, privacy or migration impact, tests run, and any live-canary evidence. Keep new dependencies minimal and pinned where practical. Security-sensitive reports belong in the private process described in `SECURITY.md`.
