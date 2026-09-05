#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-environments/dev}"

if [[ ! -d "${ROOT_DIR}/${TARGET}" ]]; then
  echo "Kustomize target does not exist: ${TARGET}" >&2
  exit 1
fi

exec kubectl kustomize "${ROOT_DIR}/${TARGET}"
