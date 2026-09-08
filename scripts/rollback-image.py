#!/usr/bin/env python3
"""Stage an explicit dev image rollback from a reviewed deploy-repository commit.

Never check out historical code: only read its overlay as data. Keep current
resources, patches, configuration and the current image/repository allowlist.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

import yaml


SPEC = importlib.util.spec_from_file_location("update_image", Path(__file__).with_name("update-image.py"))
assert SPEC and SPEC.loader
UPDATER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATER)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode:
        raise UPDATER.PromotionError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def prepare(root: Path, service_name: str, revision: str, reason: str) -> dict:
    if not UPDATER.SHA_RE.fullmatch(revision):
        raise UPDATER.PromotionError("revision must be a full 40-character deploy-repository commit SHA")
    if not reason.strip() or len(reason) > 500 or any(ord(c) < 32 for c in reason):
        raise UPDATER.PromotionError("reason must contain 1-500 characters on one line")
    # Only previously merged history is eligible, never an arbitrary PR/ref.
    git(root, "merge-base", "--is-ancestor", revision, "HEAD")
    catalog = UPDATER.load_yaml(root / "services.yaml")
    service = next((s for s in catalog["services"] if s["name"] == service_name), None)
    if service is None:
        raise UPDATER.PromotionError("unknown service")
    relative = Path(service["argocdPath"]) / "kustomization.yaml"
    if relative.as_posix() != f"environments/dev/apps/{service_name}/kustomization.yaml":
        raise UPDATER.PromotionError("unexpected overlay path")
    historical = yaml.safe_load(git(root, "show", f"{revision}:{relative.as_posix()}"))
    if not isinstance(historical, dict):
        raise UPDATER.PromotionError("historical overlay must be a mapping")
    entries = historical.get("images", [])
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise UPDATER.PromotionError("historical images must be a list of mappings")
    images = {item.get("name"): item for item in entries}
    if len(images) != len(entries):
        raise UPDATER.PromotionError("duplicate historical image names")
    annotations = historical.get("commonAnnotations") or {}
    payload = {
        "service": service_name,
        "environment": "dev",
        "source_repository": service["source"]["repository"],
        "source_sha": annotations.get("deployment.urban-assistant/source-revision"),
        "workflow_run_url": annotations.get("deployment.urban-assistant/source-workflow"),
        "images": [],
    }
    for definition in service["images"]:
        entry = images.get(definition["kustomizeName"], {})
        payload["images"].append({
            "alias": definition["alias"], "repository": entry.get("newName"), "digest": entry.get("digest")
        })
    config_revision = annotations.get("deployment.urban-assistant/config-revision")
    if config_revision is not None:
        payload["config_revision"] = config_revision
    # Same allowlist/atomic-release validation as promotion, but deliberately no
    # dev HEAD check: selecting an older source commit is the point of rollback.
    _, normalized = UPDATER.validate_payload(payload, catalog)
    current = UPDATER.load_yaml(root / relative)
    current_images = {item["name"]: item for item in current.get("images", [])}
    if all(current_images.get(i["kustomizeName"], {}).get("digest") == i["digest"] for i in normalized):
        raise UPDATER.PromotionError("selected release already has the current digests; nothing to roll back")
    UPDATER.update_overlay(root, service, normalized, payload)
    return {"payload": payload, "overlay": relative.as_posix(), "reason": reason.strip(), "revision": revision}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--service", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--payload-output", type=Path, required=True)
    parser.add_argument("--body-output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.root.resolve(), args.service, args.revision, args.reason)
    args.payload_output.write_text(json.dumps(result["payload"]), encoding="utf-8")
    args.body_output.write_text(
        f"Manual image rollback of **{args.service}** in **dev**.\n\n"
        f"- Deploy history commit: `{args.revision}`\n"
        f"- Original source commit: `{result['payload']['source_sha']}`\n"
        f"- Reason: {result['reason']}\n\n"
        "Review and merge manually. No automatic merge is enabled.\n\n"
        "Only image digests and release provenance are restored; current configuration, "
        "Vault and database schema are NOT rolled back. Migration hooks still run: confirm "
        "the old application/migrator is compatible with the current database. "
        "Pause source pushes and review other pending promotion PRs before merging.\n",
        encoding="utf-8",
    )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"overlay={result['overlay']}\n")
    print(f"Staged rollback: {result['overlay']} from {args.revision}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(f"rollback rejected: {error}") from error
