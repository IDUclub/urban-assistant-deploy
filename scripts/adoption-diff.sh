#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-environments/dev}"

if [[ ! -f "${ROOT_DIR}/${target}/kustomization.yaml" ]]; then
  echo "Kustomize target does not exist: ${target}" >&2
  exit 1
fi

echo "Read-only server-side diff for ${target}:"
kubectl diff -k "${ROOT_DIR}/${target}"
