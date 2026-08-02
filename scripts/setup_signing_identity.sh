#!/bin/zsh
set -euo pipefail

CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
SIGNING_DIR="${CODEX_ROOT}/tools/voice-memo-agent/signing"
KEYCHAIN="${SIGNING_DIR}/voice-memo-agent.keychain-db"
PASSWORD_FILE="${SIGNING_DIR}/keychain-password"
IDENTITY_NAME="Codex Local Voice Memo Agent"

for command_name in openssl security; do
  command -v "${command_name}" >/dev/null || {
    print -u2 "missing required command: ${command_name}"
    exit 1
  }
done

umask 077
mkdir -p "${SIGNING_DIR}"

if [[ ! -f "${PASSWORD_FILE}" ]]; then
  openssl rand -hex 32 > "${PASSWORD_FILE}"
fi
keychain_password="$(<"${PASSWORD_FILE}")"

if [[ ! -f "${KEYCHAIN}" ]]; then
  security create-keychain -p "${keychain_password}" "${KEYCHAIN}"
fi

keychains=()
keychain_registered=false
while IFS= read -r listed; do
  listed="${listed#*\"}"
  listed="${listed%\"*}"
  [[ -n "${listed}" ]] || continue
  keychains+=("${listed}")
  [[ "${listed}" == "${KEYCHAIN}" ]] && keychain_registered=true
done < <(security list-keychains -d user)
if [[ "${keychain_registered}" == false ]]; then
  security list-keychains -d user -s "${keychains[@]}" "${KEYCHAIN}"
fi

security unlock-keychain -p "${keychain_password}" "${KEYCHAIN}"
security set-keychain-settings -lut 21600 "${KEYCHAIN}"

identity="$(security find-identity -v -p codesigning "${KEYCHAIN}" 2>/dev/null | awk -v name="${IDENTITY_NAME}" 'index($0, name) {print $2; exit}')"
if [[ -z "${identity}" ]]; then
  temporary="${SIGNING_DIR}/create"
  rm -rf "${temporary}"
  mkdir -p "${temporary}"
  openssl req -new -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "${temporary}/key.pem" \
    -out "${temporary}/certificate.pem" \
    -subj "/CN=${IDENTITY_NAME}/O=Local Codex Automation/OU=Voice Memo Agent" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning"
  openssl pkcs12 -export -legacy \
    -inkey "${temporary}/key.pem" \
    -in "${temporary}/certificate.pem" \
    -name "${IDENTITY_NAME}" \
    -passout pass:"${keychain_password}" \
    -out "${temporary}/identity.p12"
  security import "${temporary}/identity.p12" \
    -k "${KEYCHAIN}" \
    -P "${keychain_password}" \
    -T /usr/bin/codesign >/dev/null
  security set-key-partition-list \
    -S apple-tool:,apple:,codesign: \
    -s \
    -k "${keychain_password}" \
    "${KEYCHAIN}" >/dev/null
  security find-certificate \
    -c "${IDENTITY_NAME}" \
    -p \
    "${KEYCHAIN}" > "${SIGNING_DIR}/certificate.pem"
  security add-trusted-cert \
    -r trustRoot \
    -k "${KEYCHAIN}" \
    "${SIGNING_DIR}/certificate.pem"
  rm -rf "${temporary}"
  identity="$(security find-identity -v -p codesigning "${KEYCHAIN}" | awk -v name="${IDENTITY_NAME}" 'index($0, name) {print $2; exit}')"
fi

[[ -n "${identity}" ]] || {
  print -u2 "could not create local code-signing identity"
  exit 1
}

print "${identity}"
