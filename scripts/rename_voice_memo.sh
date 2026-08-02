#!/bin/zsh
set -euo pipefail

RENAMER_APP="${VOICE_MEMO_AGENT_APP_DIR:-/Applications/Voice Memo Agent.app}"
result_file="$(mktemp -t voice-memo-renamer).json"
trap 'rm -f "${result_file}"' EXIT

[[ -d "${RENAMER_APP}" ]] || {
  print -u2 "Voice Memo Agent app is missing: ${RENAMER_APP}"
  exit 1
}

open -W -n "${RENAMER_APP}" --args --result-file "${result_file}" "$@" >/dev/null 2>&1
[[ -s "${result_file}" ]] || {
  print -u2 "Voice Memo Agent exited without a result"
  exit 1
}

cat "${result_file}"
jq -e '.ok == true' "${result_file}" >/dev/null
