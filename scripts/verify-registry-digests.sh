#!/usr/bin/env bash
set -euo pipefail

: "${PAYLOAD_JSON:?PAYLOAD_JSON must contain the validated client_payload}"

registry_scheme="${REGISTRY_SCHEME:-https}"
curl_args=(--silent --show-error --fail --head --connect-timeout 10)
if [[ "${REGISTRY_INSECURE_TLS:-true}" == true ]]; then
  curl_args+=(--insecure)
fi
if [[ -n "${REGISTRY_USERNAME:-}" ]]; then
  : "${REGISTRY_PASSWORD:?REGISTRY_PASSWORD is required with REGISTRY_USERNAME}"
  curl_args+=(--user "${REGISTRY_USERNAME}:${REGISTRY_PASSWORD}")
fi

while IFS=$'\t' read -r repository digest; do
  registry="${repository%%/*}"
  image_path="${repository#*/}"
  if [[ "${registry}" == "${repository}" || -z "${image_path}" ]]; then
    echo "Invalid registry repository: ${repository}" >&2
    exit 1
  fi
  curl "${curl_args[@]}" \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
    "${registry_scheme}://${registry}/v2/${image_path}/manifests/${digest}" >/dev/null
  echo "Verified ${repository}@${digest}"
done < <(jq -r '.images[] | [.repository, .digest] | @tsv' <<<"${PAYLOAD_JSON}")
