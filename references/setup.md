# Setup

`bootstrap.sh` installs a pinned, local copy of `jwulff/apple-voice-memo-mcp`, builds its TypeScript server and Swift Speech helper, builds the Voice Memo Agent, registers the MCP server, installs its LaunchAgent, clones the configured notes repository, and initializes local state. On first install, set `VOICE_MEMO_NOTES_REPOSITORY=owner/private-notes`. `VOICE_MEMO_NOTES_REPO_DIR` defaults to `~/Documents/VoiceMemoNotes`, and `VOICE_MEMO_NOTES_BRANCH` defaults to the GitHub repository's default branch. Upgrades reuse the repository path already installed in the LaunchAgent.

Pinned dependency:

- package manifest version: `0.1.0`
- Git commit: `f34437f546f17c78989b6e1a248d452829e50754`
- Local compatibility patch: reject null/empty audio paths without crashing memo listing and prefer the visible encrypted title over timestamp-like custom labels
- Install root: `~/.codex/tools/apple-voice-memo-mcp`

Run:

```bash
~/.codex/skills/sync-voice-memos-to-notes/scripts/bootstrap.sh
~/.codex/skills/sync-voice-memos-to-notes/scripts/doctor.sh
```

The bootstrap is idempotent. It refuses dirty tool or notes checkouts and never resets state.

Fresh state uses `publish_mode: review`, which pushes a unique branch for each memo and waits for merge. Existing state upgraded from configuration v3 or earlier retains `publish_mode: direct`; change it explicitly after reviewing the migration impact. The generated config also carries transcript, prompt, context, and subprocess limits.

## Permissions

Grant Full Disk Access in System Settings to the ChatGPT/Codex desktop app, `/Applications/Voice Memo Agent.app`, and, if macOS still denies the MCP child process, `/opt/homebrew/bin/node`. Grant Accessibility access to `/Applications/Voice Memo Agent.app`; the agent uses and verifies Voice Memos' editable title control without writing the private database. The Speech helper uses Apple's on-device `SpeechAnalyzer` API on macOS 26, which does not send memo audio to Apple's servers or require the legacy `SFSpeechRecognizer` authorization prompt. Restart the desktop app after changing MCP configuration or privacy permissions.

After bootstrap, request the one-time rename permission with:

```bash
/Applications/Voice\ Memo\ Agent.app/Contents/MacOS/VoiceMemoAgent --request-accessibility
```

Approve `Voice Memo Agent` in System Settings when prompted. Bootstrap signs the app with a persistent local certificate stored in `~/.codex/tools/voice-memo-agent/signing`, so rebuilds retain the same designated identity. Rename commands use `scripts/rename_voice_memo.sh`, which starts the approved bundle through Launch Services and returns structured CLI output.

Configure success notifications after installing the Pushover iOS app and creating an application token:

```bash
~/.codex/skills/sync-voice-memos-to-notes/scripts/configure_pushover.sh
~/.codex/skills/sync-voice-memos-to-notes/scripts/configure_pushover.sh pushover-test
```

The first command opens two native prompts. Copy each requested value from Pushover and click `Use Clipboard`; the app reads it without displaying it and immediately clears the clipboard. The signed app stores both in `~/.config/voice-memo-agent/pushover.json` with directory mode `0700` and file mode `0600`. Values are never passed through shell arguments, launchd environment values, Codex prompts, logs, or Git. Set `VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE` only when a different local path is required.

The Voice Memos data path is:

```text
~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings
```

Do not alter or rename files in that container. The MCP server opens the database read-only; title changes are submitted through the running Voice Memos app and checked by memo ID through MCP. Failed title changes remain in an independent local retry queue and never block note delivery.

## First Run

Run the coordinator once. It lists through the pinned local adapter and initializes the new-only watermark automatically:

```bash
python3 ~/.codex/skills/sync-voice-memos-to-notes/scripts/sync_voice_memos.py \
  --repo "$VOICE_MEMO_NOTES_REPO_DIR"
```

An existing baseline is never reset.
