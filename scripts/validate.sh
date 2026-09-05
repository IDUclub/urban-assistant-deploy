#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$(mktemp -d)}"
cleanup=false
if [[ $# -eq 0 ]]; then
  cleanup=true
fi
if [[ "${cleanup}" == true ]]; then
  trap 'rm -rf -- "${OUTPUT_DIR}"' EXIT
fi

python3 "${ROOT_DIR}/scripts/validate.py" --root "${ROOT_DIR}" --output-dir "${OUTPUT_DIR}"

if command -v kubeconform >/dev/null 2>&1; then
  core_schemas='https://raw.githubusercontent.com/yannh/kubernetes-json-schema/5a69f8365c9d3ed7de997f5365e22481cf775fa2/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json'
  crd_schemas='https://raw.githubusercontent.com/datreeio/CRDs-catalog/7b1e26ef9deea49293714d204c1a2270aab1178f/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
  kubeconform \
    -strict \
    -summary \
    -kubernetes-version 1.36.3 \
    -schema-location "${core_schemas}" \
    -schema-location "${crd_schemas}" \
    "${OUTPUT_DIR}"
else
  echo "kubeconform not found; repository checks passed, schema validation skipped" >&2
fi
