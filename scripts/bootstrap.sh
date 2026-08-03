#!/bin/zsh
set -euo pipefail

CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
SCRIPT_DIR="${0:A:h}"
SKILL_DIR="${SCRIPT_DIR:h}"
case "${SKILL_DIR}" in
  "${CODEX_ROOT}/worktrees/"*)
    print -u2 "refusing to install a persistent agent from an ephemeral Codex worktree: ${SKILL_DIR}"
    print -u2 "run bootstrap from a durable checkout or the installed sync-voice-memos-to-notes skill"
    exit 1
    ;;
esac
TOOL_DIR="${CODEX_ROOT}/tools/apple-voice-memo-mcp"
NOTES_REPOSITORY="${VOICE_MEMO_NOTES_REPOSITORY:-}"
NOTES_BRANCH="${VOICE_MEMO_NOTES_BRANCH:-}"
NOTES_DIR="${VOICE_MEMO_NOTES_REPO_DIR:-}"
MCP_REPOSITORY="jwulff/apple-voice-memo-mcp"
MCP_COMMIT="f34437f546f17c78989b6e1a248d452829e50754"
MCP_COMPAT_PATCH="${SKILL_DIR}/scripts/apple-voice-memo-mcp-compat.patch"
MCP_NAME="apple-voice-memos"
AGENT_BUNDLE_ID="${VOICE_MEMO_AGENT_BUNDLE_ID:-com.nrgapple.VoiceMemoAgent}"
LAUNCH_AGENT_LABEL="${VOICE_MEMO_AGENT_LAUNCH_LABEL:-${AGENT_BUNDLE_ID}}"
LAUNCH_AGENT="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
if [[ -n "${VOICE_MEMO_NODE_PATH:-}" ]]; then
  NODE_PATH="${VOICE_MEMO_NODE_PATH}"
elif [[ -x /opt/homebrew/bin/node ]]; then
  NODE_PATH="/opt/homebrew/bin/node"
else
  NODE_PATH="$(command -v node || true)"
fi
NODE_BIN_DIR="${NODE_PATH:h}"
NPM_PATH="${NODE_BIN_DIR}/npm"
PYTHON_PATH="$(command -v python3 || true)"
PUSHOVER_CREDENTIALS_FILE="${VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE:-${HOME}/.config/voice-memo-agent/pushover.json}"

for command_name in codesign codex gh git jq launchctl openssl plutil python3 rg security shasum sqlite3 swiftc; do
  command -v "${command_name}" >/dev/null || {
    print -u2 "missing required command: ${command_name}"
    exit 1
  }
done
[[ -x "${NODE_PATH}" && -x "${NPM_PATH}" ]] || {
  print -u2 "matching node/npm runtime is unavailable: ${NODE_BIN_DIR}"
  exit 1
}
node_major="$(${NODE_PATH} -p 'process.versions.node.split(".")[0]')"
(( node_major >= 18 )) || {
  print -u2 "Node.js 18 or later is required (found $(${NODE_PATH} --version))"
  exit 1
}
"${PYTHON_PATH}" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || {
  print -u2 "Python 3.9 or later is required"
  exit 1
}

if [[ -z "${NOTES_DIR}" && -f "${LAUNCH_AGENT}" ]]; then
  NOTES_DIR="$(plutil -convert json -o - "${LAUNCH_AGENT}" 2>/dev/null | jq -r '
    (.ProgramArguments | index("--repo")) as $index
    | if $index == null then "" else .ProgramArguments[$index + 1] end
  ' 2>/dev/null || true)"
fi
NOTES_DIR="${NOTES_DIR:-${HOME}/Documents/VoiceMemoNotes}"

if [[ -d "${NOTES_DIR}/.git" ]]; then
  notes_origin_for_config="$(git -C "${NOTES_DIR}" remote get-url origin)"
  if [[ -z "${NOTES_REPOSITORY}" ]]; then
    case "${notes_origin_for_config}" in
      https://github.com/*) NOTES_REPOSITORY="${notes_origin_for_config#https://github.com/}" ;;
      git@github.com:*) NOTES_REPOSITORY="${notes_origin_for_config#git@github.com:}" ;;
      *) print -u2 "set VOICE_MEMO_NOTES_REPOSITORY=owner/repository for non-GitHub origin: ${notes_origin_for_config}"; exit 1 ;;
    esac
    NOTES_REPOSITORY="${NOTES_REPOSITORY%.git}"
  fi
  if [[ -z "${NOTES_BRANCH}" ]]; then
    NOTES_BRANCH="$(git -C "${NOTES_DIR}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  fi
fi

[[ "${NOTES_REPOSITORY}" == */* ]] || {
  print -u2 "set VOICE_MEMO_NOTES_REPOSITORY=owner/private-notes before first bootstrap"
  exit 1
}
if [[ -z "${NOTES_BRANCH}" ]]; then
  NOTES_BRANCH="$(gh repo view "${NOTES_REPOSITORY}" --json defaultBranchRef --jq '.defaultBranchRef.name')"
fi
[[ -n "${NOTES_BRANCH}" ]] || {
  print -u2 "could not determine the notes repository default branch"
  exit 1
}

mkdir -p "${CODEX_ROOT}/tools"

if [[ ! -d "${TOOL_DIR}/.git" ]]; then
  gh repo clone "https://github.com/${MCP_REPOSITORY}" "${TOOL_DIR}"
else
  expected_url="https://github.com/${MCP_REPOSITORY}.git"
  actual_url="$(git -C "${TOOL_DIR}" remote get-url origin)"
  [[ "${actual_url}" == "${expected_url}" || "${actual_url}" == "git@github.com:${MCP_REPOSITORY}.git" ]] || {
    print -u2 "unexpected MCP origin: ${actual_url}"
    exit 1
  }
  mcp_dirty="$(git -C "${TOOL_DIR}" status --porcelain | rg -v '^\?\? \.codex-build/' || true)"
  [[ -z "${mcp_dirty}" ]] || {
    print -u2 "MCP checkout is dirty: ${TOOL_DIR}"
    exit 1
  }
fi

git -C "${TOOL_DIR}" fetch origin "${MCP_COMMIT}"
git -C "${TOOL_DIR}" checkout --detach "${MCP_COMMIT}"
git -C "${TOOL_DIR}" apply --check "${MCP_COMPAT_PATCH}"
git -C "${TOOL_DIR}" apply "${MCP_COMPAT_PATCH}"
git -C "${TOOL_DIR}" add src/services/voice-memo-db.ts src/tools/get-memo.ts src/tools/get-transcript.ts src/tools/list-memos.ts src/tools/transcribe.ts src/types/index.ts src/utils/paths.ts
GIT_AUTHOR_NAME="Codex Voice Memo Bootstrap" \
GIT_AUTHOR_EMAIL="codex@localhost" \
GIT_AUTHOR_DATE="2026-07-31T00:00:00Z" \
GIT_COMMITTER_NAME="Codex Voice Memo Bootstrap" \
GIT_COMMITTER_EMAIL="codex@localhost" \
GIT_COMMITTER_DATE="2026-07-31T00:00:00Z" \
  git -C "${TOOL_DIR}" commit -q -m "Apply Voice Memos null-path compatibility patch"
mcp_install_revision="$(git -C "${TOOL_DIR}" rev-parse HEAD)"
git -C "${TOOL_DIR}" update-ref refs/codex/voice-memo-compat "${mcp_install_revision}"
helper_build_dir="${TOOL_DIR}/.codex-build"
helper_path="${helper_build_dir}/VoiceMemoTranscriber"
agent_app="${VOICE_MEMO_AGENT_APP_DIR:-/Applications/Voice Memo Agent.app}"
agent_path="${agent_app}/Contents/MacOS/VoiceMemoAgent"
agent_marker="${agent_app}/Contents/Resources/source-sha256"
agent_compat_path="${helper_build_dir}/VoiceMemoAgent"
signing_keychain="${CODEX_ROOT}/tools/voice-memo-agent/signing/voice-memo-agent.keychain-db"
helper_plist="${helper_build_dir}/VoiceMemoTranscriber-Info.plist"
install_marker="${helper_build_dir}/installed-commit"
mkdir -p "${helper_build_dir}"

node_abi="$(${NODE_PATH} -p 'process.versions.modules')"
install_fingerprint="${mcp_install_revision}:${node_abi}"
if [[ -f "${install_marker}" \
  && "$(<"${install_marker}")" == "${install_fingerprint}" \
  && -f "${TOOL_DIR}/dist/index.js" \
  && -d "${TOOL_DIR}/node_modules" ]]; then
  print "MCP dependencies already built for ${mcp_install_revision}"
else
  PATH="${NODE_BIN_DIR}:${PATH}" "${NPM_PATH}" --prefix "${TOOL_DIR}" ci --prefer-offline --no-audit --no-fund
  PATH="${NODE_BIN_DIR}:${PATH}" "${NPM_PATH}" --prefix "${TOOL_DIR}" run build
  print -n "${install_fingerprint}" > "${install_marker}"
fi

cat > "${helper_plist}" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.nrgapple.VoiceMemoTranscriber</string>
<key>CFBundleName</key><string>VoiceMemoTranscriber</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSSpeechRecognitionUsageDescription</key><string>Transcribe Voice Memos into private notes.</string>
</dict></plist>
PLIST

swiftc -parse-as-library -O \
  "${SKILL_DIR}/scripts/VoiceMemoTranscriber.swift" \
  -o "${helper_path}" \
  -Xlinker -sectcreate \
  -Xlinker __TEXT \
  -Xlinker __info_plist \
  -Xlinker "${helper_plist}"

agent_fingerprint="$("${SKILL_DIR}/scripts/build_app.sh" --fingerprint)"
if [[ -n "${VOICE_MEMO_SIGNING_IDENTITY:-}" ]]; then
  signing_identity="${VOICE_MEMO_SIGNING_IDENTITY}"
  builder_signing_arguments=(--sign-identity "${signing_identity}")
else
  signing_identity="$("${SKILL_DIR}/scripts/setup_signing_identity.sh")"
  builder_signing_arguments=(--sign-identity "${signing_identity}" --keychain "${signing_keychain}")
fi
if [[ -x "${agent_path}" && -f "${agent_marker}" && "$(<"${agent_marker}")" == "${agent_fingerprint}" ]]; then
  print "Voice Memo Agent already built for ${agent_fingerprint}"
else
  agent_temp="${agent_app}.new"
  agent_build_dir="${agent_app}.build"
  rm -rf "${agent_temp}"
  rm -rf "${agent_build_dir}"
  "${SKILL_DIR}/scripts/build_app.sh" \
    --output-dir "${agent_build_dir}" \
    --architecture native \
    "${builder_signing_arguments[@]}"
  mv "${agent_build_dir}/Voice Memo Agent.app" "${agent_temp}"
  rmdir "${agent_build_dir}"
  rm -rf "${agent_app}"
  mv "${agent_temp}" "${agent_app}"
fi
ln -sfn "${agent_path}" "${agent_compat_path}"

codex mcp remove "${MCP_NAME}" >/dev/null 2>&1 || true
codex mcp add "${MCP_NAME}" \
  --env "VOICE_MEMO_TRANSCRIBER_PATH=${helper_path}" \
  -- "${NODE_PATH}" "${TOOL_DIR}/dist/index.js"

if [[ ! -d "${NOTES_DIR}/.git" ]]; then
  mkdir -p "$(dirname "${NOTES_DIR}")"
  gh repo clone "${NOTES_REPOSITORY}" "${NOTES_DIR}"
else
  notes_origin="$(git -C "${NOTES_DIR}" remote get-url origin)"
  [[ "${notes_origin}" == "https://github.com/${NOTES_REPOSITORY}" \
    || "${notes_origin}" == "https://github.com/${NOTES_REPOSITORY}.git" \
    || "${notes_origin}" == "git@github.com:${NOTES_REPOSITORY}.git" ]] || {
    print -u2 "unexpected notes origin: ${notes_origin}"
    exit 1
  }
  [[ -z "$(git -C "${NOTES_DIR}" status --porcelain)" ]] || {
    print -u2 "notes checkout is dirty: ${NOTES_DIR}"
    exit 1
  }
fi

git -C "${NOTES_DIR}" fetch origin "${NOTES_BRANCH}"
if git -C "${NOTES_DIR}" show-ref --verify --quiet "refs/heads/${NOTES_BRANCH}"; then
  git -C "${NOTES_DIR}" switch "${NOTES_BRANCH}"
else
  git -C "${NOTES_DIR}" switch --track -c "${NOTES_BRANCH}" "origin/${NOTES_BRANCH}"
fi
git -C "${NOTES_DIR}" pull --ff-only origin "${NOTES_BRANCH}"

exclude_file="${NOTES_DIR}/.git/info/exclude"
touch "${exclude_file}"
rg -Fqx '.voice-memo-automation/' "${exclude_file}" || print '.voice-memo-automation/' >> "${exclude_file}"

VOICE_MEMO_NOTES_REPOSITORY="${NOTES_REPOSITORY}" \
VOICE_MEMO_NOTES_BRANCH="${NOTES_BRANCH}" \
  python3 "${SKILL_DIR}/scripts/state.py" --repo "${NOTES_DIR}" setup

state_dir="${NOTES_DIR}/.voice-memo-automation"
mkdir -p "${HOME}/Library/LaunchAgents" "${state_dir}"
cat > "${LAUNCH_AGENT}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>${LAUNCH_AGENT_LABEL}</string>
<key>ProgramArguments</key><array>
  <string>${agent_path}</string>
  <string>watch</string>
  <string>--recordings-dir</string><string>${HOME}/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings</string>
  <string>--repo</string><string>${NOTES_DIR}</string>
  <string>--state-dir</string><string>${state_dir}</string>
  <string>--codex-path</string><string>$(command -v codex)</string>
  <string>--gh-path</string><string>$(command -v gh)</string>
  <string>--node-path</string><string>${NODE_PATH}</string>
  <string>--python-path</string><string>${PYTHON_PATH}</string>
  <string>--sync-script</string><string>${SKILL_DIR}/scripts/sync_voice_memos.py</string>
  <string>--debounce-seconds</string><string>2</string>
  <string>--reconcile-seconds</string><string>21600</string>
  <string>--sync-timeout-seconds</string><string>600</string>
</array>
<key>EnvironmentVariables</key><dict>
  <key>HOME</key><string>${HOME}</string>
  <key>CODEX_HOME</key><string>${CODEX_ROOT}</string>
  <key>VOICE_MEMO_NOTES_BRANCH</key><string>${NOTES_BRANCH}</string>
  <key>VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE</key><string>${PUSHOVER_CREDENTIALS_FILE}</string>
  <key>PATH</key><string>$(dirname "$(command -v codex)"):$(dirname "$(command -v gh)"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>${state_dir}/agent-launchd.log</string>
<key>StandardErrorPath</key><string>${state_dir}/agent-launchd.log</string>
</dict></plist>
PLIST
plutil -lint "${LAUNCH_AGENT}" >/dev/null
launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENT}"

old_app="/Applications/Voice Memo Renamer.app"
if [[ -d "${old_app}" && "${old_app}" != "${agent_app}" ]]; then
  legacy_destination="${HOME}/.Trash/Voice Memo Renamer.$(date +%Y%m%d%H%M%S).app"
  mv "${old_app}" "${legacy_destination}"
  print "moved legacy app to ${legacy_destination}"
fi

print "bootstrap complete"
print "MCP: ${MCP_NAME} (${MCP_COMMIT} + compatibility patch ${mcp_install_revision})"
print "notes: ${NOTES_DIR}"
print "agent: ${agent_app}"
print "launch agent: ${LAUNCH_AGENT}"
print "next: ${SKILL_DIR}/scripts/doctor.sh"
