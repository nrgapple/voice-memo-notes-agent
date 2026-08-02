#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
OUTPUT_DIR="${1:-${ROOT}/dist-release}"

if [[ -e "${OUTPUT_DIR}" ]]; then
  [[ -d "${OUTPUT_DIR}" && -z "$(find "${OUTPUT_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    print -u2 "release output directory must not exist or must be empty: ${OUTPUT_DIR}"
    exit 1
  }
else
  mkdir -p "${OUTPUT_DIR}"
fi

for architecture in arm64 x86_64 universal; do
  architecture_dir="${OUTPUT_DIR}/${architecture}"
  "${SCRIPT_DIR}/build_app.sh" \
    --output-dir "${architecture_dir}" \
    --architecture "${architecture}" \
    --archive
  cp "${architecture_dir}"/*.zip "${OUTPUT_DIR}/"
done

(
  cd "${OUTPUT_DIR}"
  shasum -a 256 Voice-Memo-Agent-*.zip > SHA256SUMS
)
print "release builds: ${OUTPUT_DIR}"
