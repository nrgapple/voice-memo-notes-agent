#!/bin/zsh
set -euo pipefail

CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
AGENT_BUNDLE_ID="${VOICE_MEMO_AGENT_BUNDLE_ID:-com.nrgapple.VoiceMemoAgent}"
LAUNCH_AGENT_LABEL="${VOICE_MEMO_AGENT_LAUNCH_LABEL:-${AGENT_BUNDLE_ID}}"
AGENT_APP="${VOICE_MEMO_AGENT_APP_DIR:-/Applications/Voice Memo Agent.app}"
LAUNCH_AGENT="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
SKILL_LINK="${CODEX_ROOT}/skills/sync-voice-memos-to-notes"

if [[ "${1:-}" != "--confirm" ]]; then
  print "Uninstall will stop the launch service and move these installed artifacts to the Trash:"
  print "  ${AGENT_APP}"
  print "  ${LAUNCH_AGENT}"
  print "  ${SKILL_LINK} (only when it is a symlink)"
  print "Local notes, automation state, credentials, signing material, and the MCP checkout are preserved."
  print "Run $0 --confirm to continue."
  exit 2
fi

launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT}" >/dev/null 2>&1 || true
trash_directory="${HOME}/.Trash"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ -d "${AGENT_APP}" ]]; then
  mv "${AGENT_APP}" "${trash_directory}/Voice Memo Agent.${timestamp}.app"
fi
if [[ -f "${LAUNCH_AGENT}" ]]; then
  mv "${LAUNCH_AGENT}" "${trash_directory}/${LAUNCH_AGENT_LABEL}.${timestamp}.plist"
fi
if [[ -L "${SKILL_LINK}" ]]; then
  mv "${SKILL_LINK}" "${trash_directory}/sync-voice-memos-to-notes.${timestamp}"
fi

print "Voice Memo Agent runtime artifacts moved to the Trash."
