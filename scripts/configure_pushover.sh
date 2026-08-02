#!/bin/zsh
set -euo pipefail

AGENT_APP="${VOICE_MEMO_AGENT_APP_DIR:-/Applications/Voice Memo Agent.app}"
mode="${1:-configure-pushover}"
case "${mode}" in
  configure-pushover|pushover-status|pushover-test) ;;
  *)
    print -u2 "unsupported Pushover mode: ${mode}"
    exit 2
    ;;
esac

result_file="$(mktemp -t voice-memo-pushover).json"
trap 'rm -f "${result_file}"' EXIT

[[ -d "${AGENT_APP}" ]] || {
  print -u2 "Voice Memo Agent app is missing: ${AGENT_APP}"
  exit 1
}

open -W -n "${AGENT_APP}" --args "${mode}" --result-file "${result_file}" >/dev/null 2>&1
[[ -s "${result_file}" ]] || {
  print -u2 "Voice Memo Agent exited without a result"
  exit 1
}

cat "${result_file}"
jq -e '.ok == true' "${result_file}" >/dev/null
