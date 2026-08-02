#!/bin/zsh
set -euo pipefail

CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
TARGET="${CODEX_ROOT}/skills/sync-voice-memos-to-notes"

mkdir -p "${CODEX_ROOT}/skills"

if [[ -L "${TARGET}" ]]; then
  current="$(readlink "${TARGET}")"
  if [[ "${current}" == "${REPO_ROOT}" ]]; then
    print "skill already linked: ${TARGET}"
    exit 0
  fi
  print -u2 "skill path is a symlink to a different source: ${current}"
  exit 1
fi

if [[ -e "${TARGET}" ]]; then
  backup_root="${CODEX_ROOT}/backups"
  mkdir -p "${backup_root}"
  backup="${backup_root}/sync-voice-memos-to-notes.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "${TARGET}" "${backup}"
  print "preserved previous skill at ${backup}"
fi

ln -s "${REPO_ROOT}" "${TARGET}"
print "installed skill link: ${TARGET} -> ${REPO_ROOT}"
