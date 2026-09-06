#!/usr/bin/env python3
"""Export one allowlisted service build matrix for the reusable release workflow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--catalog", default="services.yaml")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    catalog = yaml.safe_load(Path(args.catalog).read_text(encoding="utf-8"))
    service = next((item for item in catalog["services"] if item["name"] == args.service), None)
    if service is None:
        raise SystemExit(f"unknown service: {args.service}")

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if repository != service["source"]["repository"]:
        raise SystemExit(f"service {args.service} cannot be built from {repository}")
    if service["source"].get("branch") != "dev":
        raise SystemExit("service catalog must require the dev branch")
    if ref_name != "dev" or event_name != "push":
        raise SystemExit("Kubernetes releases are allowed only for push events on the protected dev branch")

    images = service["images"]
    if [item["alias"] for item in images] != service["atomicImages"]:
        raise SystemExit("catalog atomicImages differs from the build matrix")
    for image in images:
        if not re.fullmatch(r"[^/\s]+(?::\d+)?/.+", image["repository"]):
            raise SystemExit(f"invalid registry repository: {image['repository']}")

    values = {
        "images": json.dumps(images, separators=(",", ":")),
        "registry": catalog["registry"],
        "source_repository": repository,
    }
    with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
