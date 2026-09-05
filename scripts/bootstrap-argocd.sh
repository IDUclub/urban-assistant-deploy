#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

for command in kubectl helm jq; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "${command} is required" >&2
    exit 1
  fi
done

version_json="$(kubectl version -o json)"
major="$(jq -r '.serverVersion.major' <<<"${version_json}" | tr -cd '0-9')"
minor="$(jq -r '.serverVersion.minor' <<<"${version_json}" | tr -cd '0-9')"
if (( major < 1 || (major == 1 && minor < 25) )); then
  echo "Argo CD chart 10.4.1 requires Kubernetes >=1.25; cluster reports ${major}.${minor}" >&2
  exit 1
fi

echo "Installing Argo CD chart 10.4.1 (application v3.5.2) on Kubernetes ${major}.${minor}."
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update argo
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --create-namespace \
  --version 10.4.1 \
  --values "${ROOT_DIR}/argocd/bootstrap/values.yaml" \
  --wait \
  --timeout 15m

kubectl rollout status deployment/argocd-server -n argocd --timeout=10m
echo "Argo CD is installed. Add the read-only repository credential, then apply argocd/bootstrap/root-application.yaml."
