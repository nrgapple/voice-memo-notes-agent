#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
SOURCE="${ROOT}/scripts/VoiceMemoAgent.swift"
ICON="${ROOT}/assets/VoiceMemoAgent.icns"
PROJECT_VERSION="$(<"${ROOT}/VERSION")"
PROJECT_VERSION_CORE="${PROJECT_VERSION%%-*}"
if [[ "${PROJECT_VERSION}" =~ '-beta\.([0-9]+)$' ]]; then
  PROJECT_BUILD_NUMBER="${match[1]}"
else
  PROJECT_BUILD_NUMBER="1"
fi
BUNDLE_ID="${VOICE_MEMO_AGENT_BUNDLE_ID:-com.nrgapple.VoiceMemoAgent}"
MINIMUM_MACOS_VERSION="${VOICE_MEMO_MINIMUM_MACOS_VERSION:-13.0}"
OUTPUT_DIR="${ROOT}/dist"
ARCHITECTURE="universal"
SIGNING_IDENTITY="-"
SIGNING_KEYCHAIN=""
CREATE_ARCHIVE=false
PRINT_FINGERPRINT=false

usage() {
  print -u2 "usage: $0 [--output-dir DIR] [--architecture native|arm64|x86_64|universal]"
  print -u2 "          [--sign-identity IDENTITY] [--keychain PATH] [--archive] [--fingerprint]"
}

while (( $# > 0 )); do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --architecture) ARCHITECTURE="${2:-}"; shift 2 ;;
    --sign-identity) SIGNING_IDENTITY="${2:-}"; shift 2 ;;
    --keychain) SIGNING_KEYCHAIN="${2:-}"; shift 2 ;;
    --archive) CREATE_ARCHIVE=true; shift ;;
    --fingerprint) PRINT_FINGERPRINT=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

for input in "${SOURCE}" "${ICON}" "${ROOT}/VERSION"; do
  [[ -f "${input}" ]] || {
    print -u2 "missing build input: ${input}"
    exit 1
  }
done

fingerprint_hash="$(
  for fingerprint_input in "${SOURCE}" "${ICON}" "${ROOT}/VERSION" "${0:A}"; do
    shasum -a 256 "${fingerprint_input}" | awk '{print $1}'
  done
  print -r -- "${BUNDLE_ID}"
  print -r -- "${MINIMUM_MACOS_VERSION}"
)"
fingerprint_hash="$(print -r -- "${fingerprint_hash}" | shasum -a 256 | awk '{print $1}')"
fingerprint="${PROJECT_VERSION}:${fingerprint_hash}"
if [[ "${PRINT_FINGERPRINT}" == true ]]; then
  print "${fingerprint}"
  exit 0
fi

for command_name in codesign ditto lipo plutil shasum swiftc; do
  command -v "${command_name}" >/dev/null || {
    print -u2 "missing required command: ${command_name}"
    exit 1
  }
done

case "${ARCHITECTURE}" in
  native)
    ARCHITECTURE="$(uname -m)"
    ;;
  arm64|x86_64|universal) ;;
  *)
    print -u2 "unsupported architecture: ${ARCHITECTURE}"
    exit 2
    ;;
esac

[[ -n "${OUTPUT_DIR}" ]] || {
  print -u2 "output directory cannot be empty"
  exit 2
}
mkdir -p "${OUTPUT_DIR}"
APP="${OUTPUT_DIR}/Voice Memo Agent.app"
ARCHIVE="${OUTPUT_DIR}/Voice-Memo-Agent-${PROJECT_VERSION}-${ARCHITECTURE}.zip"
[[ ! -e "${APP}" ]] || {
  print -u2 "refusing to overwrite existing app: ${APP}"
  exit 1
}
if [[ "${CREATE_ARCHIVE}" == true && -e "${ARCHIVE}" ]]; then
  print -u2 "refusing to overwrite existing archive: ${ARCHIVE}"
  exit 1
fi

build_root="${OUTPUT_DIR}/.VoiceMemoAgent-build-${$}"
[[ ! -e "${build_root}" ]] || {
  print -u2 "temporary build path already exists: ${build_root}"
  exit 1
}
mkdir -p "${build_root}" "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
trap 'rm -rf -- "${build_root}"' EXIT

build_slice() {
  local architecture="$1"
  local destination="$2"
  swiftc -O \
    -target "${architecture}-apple-macosx${MINIMUM_MACOS_VERSION}" \
    "${SOURCE}" \
    -o "${destination}" \
    -framework AppKit \
    -framework ApplicationServices \
    -framework CoreServices
}

agent_binary="${APP}/Contents/MacOS/VoiceMemoAgent"
if [[ "${ARCHITECTURE}" == "universal" ]]; then
  build_slice arm64 "${build_root}/VoiceMemoAgent-arm64"
  build_slice x86_64 "${build_root}/VoiceMemoAgent-x86_64"
  lipo -create \
    "${build_root}/VoiceMemoAgent-arm64" \
    "${build_root}/VoiceMemoAgent-x86_64" \
    -output "${agent_binary}"
else
  build_slice "${ARCHITECTURE}" "${agent_binary}"
fi

cp "${ICON}" "${APP}/Contents/Resources/VoiceMemoAgent.icns"
cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>VoiceMemoAgent</string>
<key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
<key>CFBundleName</key><string>Voice Memo Agent</string>
<key>CFBundleDisplayName</key><string>Voice Memo Agent</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>VoiceMemoAgent</string>
<key>CFBundleVersion</key><string>${PROJECT_BUILD_NUMBER}</string>
<key>CFBundleShortVersionString</key><string>${PROJECT_VERSION_CORE}</string>
<key>LSMinimumSystemVersion</key><string>${MINIMUM_MACOS_VERSION}</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
print -n "${fingerprint}" > "${APP}/Contents/Resources/source-sha256"
plutil -lint "${APP}/Contents/Info.plist" >/dev/null

signing_arguments=(--force --sign "${SIGNING_IDENTITY}")
if [[ -n "${SIGNING_KEYCHAIN}" ]]; then
  signing_arguments+=(--keychain "${SIGNING_KEYCHAIN}")
fi
codesign "${signing_arguments[@]}" --identifier "${BUNDLE_ID}" "${APP}"
codesign --verify --strict "${APP}"

if [[ "${CREATE_ARCHIVE}" == true ]]; then
  ditto -c -k --sequesterRsrc --keepParent "${APP}" "${ARCHIVE}"
  shasum -a 256 "${ARCHIVE}"
fi

print "app: ${APP}"
