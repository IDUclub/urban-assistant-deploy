#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="${1:-dev}"
ENV_FILE="${ROOT_DIR}/environments/${ENVIRONMENT}/build/frontend.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing controlled frontend config: ${ENV_FILE}" >&2
  exit 1
fi

for required_name in \
  MAPBOX_PUBLIC_TOKEN \
  FRONTEND_KEYCLOAK_AUTH_URL \
  FRONTEND_KEYCLOAK_LOGOUT_REDIRECT; do
  if [[ -z "${!required_name:-}" ]]; then
    echo "${required_name} must be provided by a protected GitHub Actions secret." >&2
    exit 1
  fi
done

cat "${ENV_FILE}"
printf 'VITE_MAPBOX_TOKEN=%s\n' "${MAPBOX_PUBLIC_TOKEN}"
printf 'VITE_KEYCLOAK_AUTH_URL=%s\n' "${FRONTEND_KEYCLOAK_AUTH_URL}"
printf 'VITE_KEYCLOAK_AUTH_LOGOUT_REDIRECT=%s\n' "${FRONTEND_KEYCLOAK_LOGOUT_REDIRECT}"
