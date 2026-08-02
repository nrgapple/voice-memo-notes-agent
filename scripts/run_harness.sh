#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
MODE="${1:-fixture}"

case "${MODE}" in
  fixture|live) ;;
  *)
    print -u2 "usage: $0 [fixture|live]"
    exit 2
    ;;
esac

cd "${ROOT}"
python3 -m py_compile scripts/*.py
python3 scripts/validate_project.py
zsh -n scripts/*.sh
swiftc -typecheck scripts/VoiceMemoAgent.swift \
  -framework AppKit \
  -framework ApplicationServices \
  -framework CoreServices
swiftc -parse-as-library -typecheck scripts/VoiceMemoTranscriber.swift
agent_test_binary="$(mktemp -t VoiceMemoAgent-tests)"
trap 'rm -f "${agent_test_binary}"' EXIT
swiftc scripts/VoiceMemoAgent.swift \
  -o "${agent_test_binary}" \
  -framework AppKit \
  -framework ApplicationServices \
  -framework CoreServices
VOICE_MEMO_AGENT_TEST_BINARY="${agent_test_binary}" python3 scripts/test_helpers.py
git diff --check

if [[ "${MODE}" == "live" ]]; then
  scripts/doctor.sh
fi

print "product harness passed (${MODE})"
