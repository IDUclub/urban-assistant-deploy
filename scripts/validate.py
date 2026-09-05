#!/usr/bin/env python3
"""Repository-specific deterministic checks for rendered Kubernetes state."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml


ALLOWED_PLACEHOLDERS = {
    "${pvc.metadata.namespace}-${pvc.metadata.name}-${pv.metadata.name}",
    "${env:MY_POD_IP}",
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def render(root: Path, target: Path) -> str:
    result = subprocess.run(
        ["kubectl", "kustomize", str(target)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"kustomize build failed for {target.relative_to(root)}:\n{result.stderr}")
    return result.stdout


def walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from mappings(child)


def objects(text: str, source: str, errors: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        documents = yaml.safe_load_all(text)
        for index, document in enumerate(documents, 1):
            if document is None:
                continue
            if not isinstance(document, dict):
                fail(errors, f"{source} document {index} is not a Kubernetes object")
                continue
            result.append(document)
    except yaml.YAMLError as error:
        fail(errors, f"invalid YAML in {source}: {error}")
    return result


def validate_sources(root: Path, errors: list[str]) -> None:
    roots = ["apps", "argocd", "cluster", "environments", "operators", "platform"]
    for directory in roots:
        for path in (root / directory).rglob("*"):
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(root).as_posix()
            if "CHANGE_ME" in text:
                fail(errors, f"forbidden CHANGE_ME marker in {relative}")
            scrubbed = text
            for allowed in ALLOWED_PLACEHOLDERS:
                scrubbed = scrubbed.replace(allowed, "")
            if "${" in scrubbed:
                fail(errors, f"unresolved substitution placeholder in {relative}")
            if re.search(r"^\s*image:\s*\S+:latest(?:\s|$)", text, re.MULTILINE):
                fail(errors, f"mutable latest image in {relative}")


def validate_catalog(root: Path, errors: list[str]) -> dict[str, Any]:
    catalog_path = root / "services.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("kind") != "ServiceCatalog":
        fail(errors, "services.yaml is not a ServiceCatalog")
        return {}
    registry = catalog.get("registry")
    if not isinstance(registry, str) or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", registry):
        fail(errors, "services.yaml registry must be a hostname with an optional port and no URL scheme")
    frontend_env = (root / "environments/dev/build/frontend.env").read_text(encoding="utf-8")
    for key in catalog.get("policies", {}).get("githubSecretBuildKeys", []):
        if re.search(rf"(?m)^\s*{re.escape(str(key))}=", frontend_env):
            fail(errors, f"GitHub-secret build key {key} is stored in environments/dev/build/frontend.env")
    names: set[str] = set()
    for service in catalog.get("services", []):
        name = service.get("name")
        if not name or name in names:
            fail(errors, f"duplicate or empty service name in services.yaml: {name!r}")
            continue
        names.add(name)
        if service.get("source", {}).get("branch") != "dev":
            fail(errors, f"{name}: release branch must be dev")
        overlay = root / service.get("argocdPath", "") / "kustomization.yaml"
        if not overlay.is_file():
            fail(errors, f"{name}: missing overlay {overlay.relative_to(root)}")
            continue
        configuration = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        configured_images = {image.get("name"): image for image in configuration.get("images", [])}
        aliases = [image.get("alias") for image in service.get("images", [])]
        if aliases != service.get("atomicImages"):
            fail(errors, f"{name}: atomicImages must list every image alias in catalog order")
        for image in service.get("images", []):
            configured = configured_images.get(image.get("kustomizeName"))
            if not configured:
                fail(errors, f"{name}: overlay lacks {image.get('kustomizeName')}")
                continue
            if configured.get("newName") != image.get("repository"):
                fail(errors, f"{name}: overlay repository differs for {image.get('alias')}")
            digest = configured.get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                fail(errors, f"{name}: {image.get('alias')} has no exact sha256 digest")
    return catalog


def is_internal_endpoint(value: str) -> bool:
    stripped = value.strip("'\"")
    if re.fullmatch(r"0\.0\.0\.0(?::\d+)?", stripped):
        return True
    parsed = urlsplit(stripped)
    host = parsed.hostname
    return bool(host and (host == "0.0.0.0" or "." not in host or host.endswith(".svc.cluster.local")))


def validate_vault_contract(root: Path, rendered: list[dict[str, Any]], errors: list[str]) -> None:
    contract_path = root / "vault-contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or contract.get("kind") != "VaultContract":
        fail(errors, "vault-contract.yaml is not a VaultContract")
        return

    references: dict[str, set[str]] = defaultdict(set)
    mounts: dict[str, set[str]] = defaultdict(set)
    endpoint_literal = re.compile(
        r"(?:https?|redis|postgresql(?:\+\w+)?|mongodb(?:\+srv)?|bolt)://\S+"
        r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"
    )
    for document in rendered:
        if document.get("kind") != "VaultStaticSecret":
            continue
        spec = document.get("spec") or {}
        path = str(spec.get("path", ""))
        mounts[path].add(str(spec.get("mount", "")))
        templates = (((spec.get("destination") or {}).get("transformation") or {}).get("templates") or {})
        for template in templates.values() if isinstance(templates, dict) else []:
            text = str((template or {}).get("text", ""))
            references[path].update(re.findall(r'get \.Secrets "([^"]+)"', text))
            normalized = re.sub(r"\{\{.*?\}\}", "secret", text)
            for endpoint in endpoint_literal.findall(normalized):
                if not is_internal_endpoint(endpoint):
                    fail(errors, f"literal external endpoint is embedded in VaultStaticSecret for path {path!r}")

    paths = contract.get("paths") or {}
    if not isinstance(paths, dict):
        fail(errors, "vault-contract.yaml paths must be a mapping")
        return
    contract_mount = str(contract.get("mount", ""))
    if not contract_mount:
        fail(errors, "vault-contract.yaml mount must be a non-empty string")

    declared: dict[str, set[str]] = {}
    for path, keys in paths.items():
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            fail(errors, f"vault-contract.yaml path {path!r} must contain a list of keys")
            continue
        if len(keys) != len(set(keys)):
            fail(errors, f"vault-contract.yaml path {path!r} contains duplicate keys")
        if keys != sorted(keys):
            fail(errors, f"vault-contract.yaml path {path!r} keys must be sorted")
        declared[str(path)] = set(keys)

    for path in sorted(set(declared) | set(references)):
        unused = sorted(declared.get(path, set()) - references.get(path, set()))
        undocumented = sorted(references.get(path, set()) - declared.get(path, set()))
        if unused:
            fail(errors, f"Vault contract {path!r} contains unused keys: {', '.join(unused)}")
        if undocumented:
            fail(errors, f"Vault templates for {path!r} use undocumented keys: {', '.join(undocumented)}")
        unexpected_mounts = sorted(mount for mount in mounts.get(path, set()) if mount != contract_mount)
        if unexpected_mounts:
            fail(
                errors,
                f"Vault path {path!r} uses mounts outside contract mount {contract_mount!r}: "
                f"{', '.join(unexpected_mounts)}",
            )


def validate_rendered(root: Path, text: str, catalog: dict[str, Any], errors: list[str]) -> None:
    rendered = objects(text, "environments/dev", errors)
    owners: dict[tuple[str, str, str, str], int] = defaultdict(int)
    node_ports: dict[int, list[str]] = defaultdict(list)
    vault_paths: dict[str, list[str]] = defaultdict(list)
    vault_only_keys = set(catalog.get("policies", {}).get("vaultOnlyEnvironmentKeys", []))
    registry = str(catalog.get("registry", "")).rstrip("/")
    first_party_prefix = f"{registry}/" if registry else ""

    for document in rendered:
        api_version = str(document.get("apiVersion", ""))
        kind = str(document.get("kind", ""))
        metadata = document.get("metadata") or {}
        name = str(metadata.get("name", ""))
        namespace = str(metadata.get("namespace", ""))
        identity = (api_version, kind, namespace, name)
        owners[identity] += 1

        if kind == "ConfigMap":
            data = document.get("data") or {}
            if isinstance(data, dict):
                for key in sorted(vault_only_keys.intersection(data)):
                    fail(errors, f"Vault-only environment key {key} is stored in ConfigMap {namespace}/{name}")
                for data_key, value in data.items():
                    if not isinstance(value, str):
                        continue
                    for key in sorted(vault_only_keys):
                        if re.search(rf"(?m)^\s*{re.escape(key)}=", value):
                            fail(
                                errors,
                                f"Vault-only environment key {key} is stored in ConfigMap "
                                f"{namespace}/{name} data.{data_key}",
                            )
        if kind == "Secret" and ("data" in document or "stringData" in document):
            fail(errors, f"forbidden Secret payload in {namespace}/{name}")
        if kind == "VaultStaticSecret":
            vault_paths[str(document.get("spec", {}).get("path", ""))].append(name)

        for mapping in mappings(document):
            env_name = mapping.get("name")
            if env_name in vault_only_keys and "value" in mapping:
                fail(errors, f"Vault-only environment key {env_name} has a literal value in {kind}/{namespace}/{name}")

        for key, value in walk(document):
            if key == "nodePort" and isinstance(value, int):
                node_ports[value].append(f"{kind}/{namespace}/{name}")
            if key == "image" and isinstance(value, str):
                if value.endswith(":latest"):
                    fail(errors, f"mutable latest image in {kind}/{namespace}/{name}: {value}")
                if first_party_prefix and value.startswith(first_party_prefix) and not re.search(
                    r"@sha256:[0-9a-f]{64}$", value
                ):
                    fail(errors, f"first-party image is not digest-pinned in {kind}/{namespace}/{name}: {value}")
                if value.startswith("urban-assistant/"):
                    fail(errors, f"logical first-party alias escaped overlay in {kind}/{namespace}/{name}: {value}")

    for identity, count in owners.items():
        if count > 1:
            fail(errors, f"resource has multiple owners in full render: {identity} ({count} copies)")
    for port, resources in node_ports.items():
        if len(resources) > 1:
            fail(errors, f"NodePort {port} is duplicated by {', '.join(resources)}")

    allowed_shared = catalog.get("policies", {}).get("allowedSharedVaultPaths", {})
    for path, names in vault_paths.items():
        if len(names) > 1 and sorted(names) != sorted(allowed_shared.get(path, [])):
            fail(errors, f"Vault path {path!r} is shared unexpectedly by {', '.join(names)}")

    validate_vault_contract(root, rendered, errors)


def target_paths(root: Path) -> list[Path]:
    targets = [root / "environments/dev", root / "argocd/root", root / "argocd/adoption"]
    targets.extend(sorted((root / "environments/dev/apps").iterdir()))
    targets.extend(sorted((root / "environments/dev/prerequisites").iterdir()))
    targets.extend(sorted((root / "environments/dev/platform").iterdir()))
    targets.append(root / "environments/dev/cluster")
    return [path for path in targets if (path / "kustomization.yaml").is_file()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    validate_sources(root, errors)
    catalog = validate_catalog(root, errors)
    renders: dict[str, str] = {}
    try:
        for target in target_paths(root):
            relative = target.relative_to(root).as_posix()
            rendered = render(root, target)
            renders[relative] = rendered
            if output_dir:
                filename = relative.replace("/", "__") + ".yaml"
                (output_dir / filename).write_text(rendered, encoding="utf-8", newline="\n")
    except RuntimeError as error:
        fail(errors, str(error))

    if "environments/dev" in renders:
        validate_rendered(root, renders["environments/dev"], catalog, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(renders)} Kustomize targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
