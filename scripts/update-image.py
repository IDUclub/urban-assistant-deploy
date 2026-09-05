#!/usr/bin/env python3
"""Validate a promotion event and update one dev Kustomize overlay atomically."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONFIG_REVISION_RE = re.compile(r"^[0-9A-Za-z._/-]{1,128}$")


class PromotionError(ValueError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise PromotionError(f"{path} must contain a YAML mapping")
    return value


def load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json:
        value = json.loads(args.payload_json)
    else:
        with Path(args.payload).open(encoding="utf-8") as stream:
            value = json.load(stream)
    if not isinstance(value, dict):
        raise PromotionError("client_payload must be a JSON object")
    return value


def validate_payload(payload: dict[str, Any], catalog: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    required = {"service", "environment", "source_repository", "source_sha", "images", "workflow_run_url"}
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - required - {"config_revision"})
    if missing:
        raise PromotionError(f"missing payload fields: {', '.join(missing)}")
    if unknown:
        raise PromotionError(f"unknown payload fields: {', '.join(unknown)}")
    if payload["environment"] != catalog.get("environment") or payload["environment"] != "dev":
        raise PromotionError("only the dev environment can be updated automatically")

    service = next((item for item in catalog.get("services", []) if item.get("name") == payload["service"]), None)
    if service is None:
        raise PromotionError(f"unknown service: {payload['service']!r}")
    if payload["source_repository"] != service["source"]["repository"]:
        raise PromotionError("source_repository is not allowlisted for this service")
    if service["source"].get("branch") != "dev":
        raise PromotionError("service catalog must require the dev branch")
    if not isinstance(payload["source_sha"], str) or not SHA_RE.fullmatch(payload["source_sha"]):
        raise PromotionError("source_sha must be 40 lowercase hexadecimal characters")

    workflow_prefix = f"https://github.com/{payload['source_repository']}/actions/runs/"
    if not isinstance(payload["workflow_run_url"], str) or not payload["workflow_run_url"].startswith(workflow_prefix):
        raise PromotionError("workflow_run_url must point to the allowlisted source repository")

    config_revision = payload.get("config_revision")
    if config_revision is not None:
        if not isinstance(config_revision, str) or not CONFIG_REVISION_RE.fullmatch(config_revision):
            raise PromotionError("config_revision contains unsupported characters")
        if service["name"] != "frontend":
            raise PromotionError("config_revision is supported only for frontend promotions")

    images = payload["images"]
    if not isinstance(images, list) or not images:
        raise PromotionError("images must be a non-empty array")
    expected_aliases = service.get("atomicImages", [])
    provided_aliases = [image.get("alias") for image in images if isinstance(image, dict)]
    if len(provided_aliases) != len(images) or len(set(provided_aliases)) != len(provided_aliases):
        raise PromotionError("each image must be an object with a unique alias")
    if set(provided_aliases) != set(expected_aliases) or len(provided_aliases) != len(expected_aliases):
        raise PromotionError(f"release must update exactly these aliases: {', '.join(expected_aliases)}")

    allowed = {image["alias"]: image for image in service.get("images", [])}
    normalized: list[dict[str, str]] = []
    for image in images:
        alias = image["alias"]
        if set(image) != {"alias", "repository", "digest"}:
            raise PromotionError(f"image {alias!r} must contain only alias, repository and digest")
        definition = allowed.get(alias)
        if definition is None or image["repository"] != definition["repository"]:
            raise PromotionError(f"repository is not allowlisted for image alias {alias!r}")
        if not isinstance(image["digest"], str) or not DIGEST_RE.fullmatch(image["digest"]):
            raise PromotionError(f"invalid sha256 digest for image alias {alias!r}")
        normalized.append({
            "alias": alias,
            "repository": image["repository"],
            "digest": image["digest"],
            "kustomizeName": definition["kustomizeName"],
        })
    return service, normalized


def update_overlay(root: Path, service: dict[str, Any], images: list[dict[str, str]], payload: dict[str, Any]) -> Path:
    overlay = root / service["argocdPath"] / "kustomization.yaml"
    document = load_yaml(overlay)
    configured = {item.get("name"): item for item in document.get("images", []) if isinstance(item, dict)}
    for image in images:
        entry = configured.get(image["kustomizeName"])
        if entry is None:
            raise PromotionError(f"overlay is missing Kustomize image {image['kustomizeName']!r}")
        if entry.get("newName") != image["repository"]:
            raise PromotionError(f"overlay repository differs from services.yaml for {image['alias']!r}")
        entry.pop("newTag", None)
        entry["digest"] = image["digest"]

    annotations = document.setdefault("commonAnnotations", {})
    if not isinstance(annotations, dict):
        raise PromotionError("commonAnnotations in the target overlay must be a mapping")
    annotations["deployment.urban-assistant/source-revision"] = payload["source_sha"]
    annotations["deployment.urban-assistant/source-workflow"] = payload["workflow_run_url"]
    if payload.get("config_revision"):
        annotations["deployment.urban-assistant/config-revision"] = payload["config_revision"]

    serialized = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=120)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=overlay.parent, delete=False) as stream:
        stream.write(serialized)
        temporary = Path(stream.name)
    os.replace(temporary, overlay)
    return overlay


def emit_github_output(path: Path, service: dict[str, Any], payload: dict[str, Any], overlay: Path, root: Path) -> None:
    owner, repository = payload["source_repository"].split("/", 1)
    values = {
        "service": service["name"],
        "source_repository": payload["source_repository"],
        "source_owner": owner,
        "source_name": repository,
        "source_sha": payload["source_sha"],
        "overlay": overlay.relative_to(root).as_posix(),
        "branch_name": f"deploy/dev-{service['name']}-{payload['source_sha']}",
        "pr_title": f"deploy(dev): {service['name']} {payload['source_sha'][:12]}",
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--payload")
    source.add_argument("--payload-json")
    parser.add_argument("--catalog", default="services.yaml")
    parser.add_argument("--root", default=".")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    catalog = load_yaml((root / args.catalog).resolve())
    payload = load_payload(args)
    service, images = validate_payload(payload, catalog)
    overlay = update_overlay(root, service, images, payload)
    if args.github_output:
        emit_github_output(args.github_output, service, payload, overlay, root)
    print(f"updated {overlay.relative_to(root).as_posix()} with {len(images)} immutable digest(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, yaml.YAMLError, PromotionError) as error:
        raise SystemExit(f"promotion rejected: {error}") from error
