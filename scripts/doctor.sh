#!/bin/zsh
set -u

CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
SCRIPT_DIR="${0:A:h}"
SKILL_DIR="${SCRIPT_DIR:h}"
TOOL_DIR="${CODEX_ROOT}/tools/apple-voice-memo-mcp"
NOTES_REPOSITORY="${VOICE_MEMO_NOTES_REPOSITORY:-}"
NOTES_BRANCH="${VOICE_MEMO_NOTES_BRANCH:-}"
NOTES_DIR="${VOICE_MEMO_NOTES_REPO_DIR:-}"
MCP_COMMIT="f34437f546f17c78989b6e1a248d452829e50754"
MCP_COMPAT_PATCH="${SKILL_DIR}/scripts/apple-voice-memo-mcp-compat.patch"
AGENT_APP="${VOICE_MEMO_AGENT_APP_DIR:-/Applications/Voice Memo Agent.app}"
AGENT_PATH="${AGENT_APP}/Contents/MacOS/VoiceMemoAgent"
AGENT_CLI="${SKILL_DIR}/scripts/rename_voice_memo.sh"
PUSHOVER_CLI="${SKILL_DIR}/scripts/configure_pushover.sh"
AGENT_KEYCHAIN="${CODEX_ROOT}/tools/voice-memo-agent/signing/voice-memo-agent.keychain-db"
AGENT_BUNDLE_ID="${VOICE_MEMO_AGENT_BUNDLE_ID:-com.nrgapple.VoiceMemoAgent}"
LAUNCH_AGENT_LABEL="${VOICE_MEMO_AGENT_LAUNCH_LABEL:-${AGENT_BUNDLE_ID}}"
LAUNCH_AGENT="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
RECORDINGS_DIR="${HOME}/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
PUSHOVER_CREDENTIALS_FILE="${VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE:-${HOME}/.config/voice-memo-agent/pushover.json}"
if [[ -n "${VOICE_MEMO_NODE_PATH:-}" ]]; then
  NODE_PATH="${VOICE_MEMO_NODE_PATH}"
elif [[ -x /opt/homebrew/bin/node ]]; then
  NODE_PATH="/opt/homebrew/bin/node"
else
  NODE_PATH="$(command -v node)"
fi
failures=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    print "PASS ${label}"
  else
    print "FAIL ${label}"
    failures=$((failures + 1))
  fi
}

for command_name in codesign codex gh git jq launchctl node npm openssl plutil python3 security shasum sqlite3 swiftc rg; do
  check "command:${command_name}" command -v "${command_name}"
done

if [[ -z "${NOTES_DIR}" && -f "${LAUNCH_AGENT}" ]]; then
  NOTES_DIR="$(plutil -convert json -o - "${LAUNCH_AGENT}" 2>/dev/null | jq -r '
    (.ProgramArguments | index("--repo")) as $index
    | if $index == null then "" else .ProgramArguments[$index + 1] end
  ' 2>/dev/null || true)"
fi
NOTES_DIR="${NOTES_DIR:-${HOME}/Documents/VoiceMemoNotes}"
if [[ -d "${NOTES_DIR}/.git" ]]; then
  notes_origin_for_config="$(git -C "${NOTES_DIR}" remote get-url origin 2>/dev/null || true)"
  if [[ -z "${NOTES_REPOSITORY}" ]]; then
    case "${notes_origin_for_config}" in
      https://github.com/*) NOTES_REPOSITORY="${notes_origin_for_config#https://github.com/}" ;;
      git@github.com:*) NOTES_REPOSITORY="${notes_origin_for_config#git@github.com:}" ;;
    esac
    NOTES_REPOSITORY="${NOTES_REPOSITORY%.git}"
  fi
  if [[ -z "${NOTES_BRANCH}" ]]; then
    NOTES_BRANCH="$(jq -r '.branch // empty' "${NOTES_DIR}/.voice-memo-automation/config.json" 2>/dev/null || true)"
  fi
fi

check "github-auth" gh auth status
check "codex-auth" codex login status
check "node-version" "${NODE_PATH}" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)'
check "python-version" python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'
check "mcp-source" test -f "${TOOL_DIR}/dist/index.js"
check "voice-memo-cli" "${NODE_PATH}" "${SKILL_DIR}/scripts/voice_memo_cli.mjs" list
check "sync-coordinator" python3 "${SKILL_DIR}/scripts/sync_voice_memos.py" --help
check "benchmark-cli" python3 "${SKILL_DIR}/scripts/benchmark.py" --help
check "node-native-abi" zsh -c "'${NODE_PATH}' -e \"import('${TOOL_DIR}/dist/services/voice-memo-db.js').then(({VoiceMemoDatabase}) => { const db = new VoiceMemoDatabase(); db.listMemos({limit: 1}); db.close(); })\""
check "speech-helper" test -x "${TOOL_DIR}/.codex-build/VoiceMemoTranscriber"
check "speech-helper-metadata" zsh -c "otool -l '${TOOL_DIR}/.codex-build/VoiceMemoTranscriber' | rg -q '__info_plist'"
check "speech-helper-cli" "${TOOL_DIR}/.codex-build/VoiceMemoTranscriber" --help
check "agent-binary" test -x "${AGENT_PATH}"
check "agent-signature" codesign --verify --strict "${AGENT_APP}"
agent_fingerprint="$("${SKILL_DIR}/scripts/build_app.sh" --fingerprint)"
check "agent-source-version" zsh -c "test \"\$(cat '${AGENT_APP}/Contents/Resources/source-sha256')\" = '${agent_fingerprint}'"
if [[ -n "${VOICE_MEMO_SIGNING_IDENTITY:-}" ]]; then
  check "agent-signing-identity" zsh -c "codesign -dvv '${AGENT_APP}' 2>&1 | rg -Fq 'Authority=${VOICE_MEMO_SIGNING_IDENTITY}'"
else
  check "agent-local-identity" zsh -c "security find-identity -v -p codesigning '${AGENT_KEYCHAIN}' | rg -q 'Codex Local Voice Memo Agent'"
  check "agent-not-adhoc" zsh -c "codesign -dvv '${AGENT_APP}' 2>&1 | rg -q 'Authority=Codex Local Voice Memo Agent'"
fi
check "agent-cli" "${AGENT_PATH}" --help
check "agent-created-event" zsh -c "'${AGENT_PATH}' classify-event --path '/tmp/new.m4a' --created --is-file | jq -e '.should_trigger == true'"
check "agent-modified-event" zsh -c "'${AGENT_PATH}' classify-event --path '/tmp/existing.m4a' --is-file | jq -e '.should_trigger == false'"
check "agent-accessibility" "${AGENT_CLI}" --check-accessibility
check "agent-full-disk-access" zsh -c "'${AGENT_CLI}' doctor --recordings-dir '${RECORDINGS_DIR}' --codex-path '$(command -v codex)' --gh-path '$(command -v gh)' | jq -e '.full_disk_access == true'"
if "${PUSHOVER_CLI}" pushover-status 2>/dev/null | jq -e '.configured == true' >/dev/null; then
  print "PASS pushover-credentials"
else
  print "FAIL pushover-credentials"
  print -u2 "Pushover credentials are missing, invalid, or not mode 0600: ${PUSHOVER_CREDENTIALS_FILE}"
  failures=$((failures + 1))
fi
check "agent-launch-plist" plutil -lint "${LAUNCH_AGENT}"
check "agent-launch-service" launchctl print "gui/$(id -u)/${LAUNCH_AGENT_LABEL}"
check "agent-sync-timeout" zsh -c "plutil -convert json -o - '${LAUNCH_AGENT}' | jq -e '.ProgramArguments | index(\"--sync-timeout-seconds\") != null'"
check "mcp-registered" zsh -c "codex mcp list --json | jq -e '.[] | select(.name == \"apple-voice-memos\" and .enabled == true)'"
check "mcp-pinned-base" zsh -c "test \"\$(git -C '${TOOL_DIR}' rev-parse HEAD^)\" = '${MCP_COMMIT}'"
check "mcp-compat-patch" git -C "${TOOL_DIR}" apply --reverse --check "${MCP_COMPAT_PATCH}"
check "notes-checkout" git -C "${NOTES_DIR}" rev-parse --is-inside-work-tree
if [[ -n "${NOTES_REPOSITORY}" ]]; then
  check "notes-origin" zsh -c "git -C '${NOTES_DIR}' remote get-url origin | rg -q 'github.com[:/]${NOTES_REPOSITORY}(.git)?$'"
else
  print "FAIL notes-origin"
  failures=$((failures + 1))
fi
if [[ -n "${NOTES_BRANCH}" ]]; then
  check "notes-branch" zsh -c "test \"\$(git -C '${NOTES_DIR}' branch --show-current)\" = '${NOTES_BRANCH}'"
else
  print "FAIL notes-branch"
  failures=$((failures + 1))
fi
check "notes-clean" zsh -c "test -z \"\$(git -C '${NOTES_DIR}' status --porcelain)\""
check "state" test -f "${NOTES_DIR}/.voice-memo-automation/state.json"
check "voice-memos-container" test -d "${RECORDINGS_DIR}"
check "voice-memos-readable" zsh -c "ls '${RECORDINGS_DIR}' >/dev/null"

if (( failures > 0 )); then
  print -u2 "doctor found ${failures} failing check(s)"
  print -u2 "Voice Memos access failures require Full Disk Access for ChatGPT/Codex and possibly node."
  print -u2 "Agent access failures require Accessibility and Full Disk Access for Voice Memo Agent."
  exit 1
fi

print "doctor passed"
