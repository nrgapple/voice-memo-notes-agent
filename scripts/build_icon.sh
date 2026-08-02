#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
SOURCE="${1:-${ROOT}/assets/VoiceMemoAgent.icon.png}"
OUTPUT="${2:-${ROOT}/assets/VoiceMemoAgent.icns}"

for command_name in iconutil sips; do
  command -v "${command_name}" >/dev/null || {
    print -u2 "missing required command: ${command_name}"
    exit 1
  }
done

[[ -f "${SOURCE}" ]] || {
  print -u2 "icon source not found: ${SOURCE}"
  exit 1
}

width="$(sips -g pixelWidth "${SOURCE}" | awk '/pixelWidth:/ {print $2}')"
height="$(sips -g pixelHeight "${SOURCE}" | awk '/pixelHeight:/ {print $2}')"
[[ "${width}" == "1024" && "${height}" == "1024" ]] || {
  print -u2 "icon source must be 1024x1024 (found ${width}x${height})"
  exit 1
}

temp_root="$(mktemp -d "${TMPDIR:-/tmp}/VoiceMemoAgent-icon.XXXXXX")"
trap 'rm -rf -- "${temp_root}"' EXIT
iconset="${temp_root}/VoiceMemoAgent.iconset"
mkdir -p "${iconset}" "${OUTPUT:h}"

for specification in \
  "16 icon_16x16.png" \
  "32 icon_16x16@2x.png" \
  "32 icon_32x32.png" \
  "64 icon_32x32@2x.png" \
  "128 icon_128x128.png" \
  "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" \
  "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" \
  "1024 icon_512x512@2x.png"; do
  size="${specification%% *}"
  filename="${specification#* }"
  sips -z "${size}" "${size}" "${SOURCE}" --out "${iconset}/${filename}" >/dev/null
done

generated_icon="${temp_root}/VoiceMemoAgent.icns"
iconutil -c icns "${iconset}" -o "${generated_icon}"
mv "${generated_icon}" "${OUTPUT}"
print "icon: ${OUTPUT}"
