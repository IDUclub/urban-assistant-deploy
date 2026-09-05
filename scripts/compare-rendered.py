#!/usr/bin/env python3
"""Compare Kubernetes object identities between two rendered YAML streams."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


Identity = tuple[str, str, str, str]


def load(path: Path) -> dict[Identity, dict[str, Any]]:
    result: dict[Identity, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as stream:
        for document in yaml.safe_load_all(stream):
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata") or {}
            identity = (
                str(document.get("apiVersion", "")),
                str(document.get("kind", "")),
                str(metadata.get("namespace", "")),
                str(metadata.get("name", "")),
            )
            if identity in result:
                raise SystemExit(f"duplicate identity in {path}: {identity}")
            result[identity] = document
    return result


def label(identity: Identity) -> str:
    api_version, kind, namespace, name = identity
    scope = f"{namespace}/" if namespace else ""
    return f"{api_version} {kind} {scope}{name}"


def critical_state(document: dict[str, Any]) -> Any:
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "Service":
        return {
            "selector": spec.get("selector"),
            "ports": [
                {key: port.get(key) for key in ("name", "port", "targetPort", "nodePort") if key in port}
                for port in spec.get("ports", [])
            ],
        }
    if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        return {"selector": spec.get("selector"), "serviceName": spec.get("serviceName")}
    if kind == "PersistentVolumeClaim":
        return spec
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--fail-on-removed", action="store_true")
    args = parser.parse_args()
    before = load(args.before)
    after = load(args.after)
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    changed = []
    for identity in sorted(before.keys() & after.keys()):
        old_state = critical_state(before[identity])
        new_state = critical_state(after[identity])
        if old_state is not None and old_state != new_state:
            changed.append(identity)
    print(
        f"before={len(before)} after={len(after)} added={len(added)} "
        f"removed={len(removed)} critical_changed={len(changed)}"
    )
    for identity in added:
        print(f"ADDED   {label(identity)}")
    for identity in removed:
        print(f"REMOVED {label(identity)}")
    for identity in changed:
        print(f"CHANGED {label(identity)}")
    return 1 if args.fail_on_removed and (removed or changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
