from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "export-build-contract.py"
SPEC = importlib.util.spec_from_file_location("export_build_contract", SCRIPT)
assert SPEC and SPEC.loader
EXPORT_BUILD_CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT_BUILD_CONTRACT)


class ExportBuildContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.catalog_path = self.root / "services.yaml"
        self.output_path = self.root / "github-output"
        self.catalog = {
            "registry": "registry.local:5000",
            "services": [
                {
                    "name": "example",
                    "source": {"repository": "IDUclub/example", "branch": "dev"},
                    "atomicImages": ["api"],
                    "images": [
                        {
                            "alias": "api",
                            "repository": "registry.local:5000/example",
                            "context": ".",
                            "dockerfile": "Dockerfile",
                            "target": None,
                        }
                    ],
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_export(self, *, repository: str = "IDUclub/example", ref: str = "dev", event: str = "push") -> int:
        self.catalog_path.write_text(yaml.safe_dump(self.catalog), encoding="utf-8")
        argv = [
            str(SCRIPT),
            "--service",
            "example",
            "--catalog",
            str(self.catalog_path),
            "--github-output",
            str(self.output_path),
        ]
        environment = {
            "GITHUB_REPOSITORY": repository,
            "GITHUB_REF_NAME": ref,
            "GITHUB_EVENT_NAME": event,
        }
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, environment, clear=True):
            return EXPORT_BUILD_CONTRACT.main()

    def test_exports_contract_for_push_to_dev(self) -> None:
        self.assertEqual(self.run_export(), 0)
        values = dict(
            line.split("=", 1)
            for line in self.output_path.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(values["registry"], "registry.local:5000")
        self.assertEqual(json.loads(values["images"])[0]["alias"], "api")

    def test_rejects_non_dev_source_branch_in_catalog(self) -> None:
        self.catalog["services"][0]["source"]["branch"] = "develop"
        with self.assertRaisesRegex(SystemExit, "catalog must require the dev branch"):
            self.run_export(ref="develop")

    def test_rejects_push_from_another_branch(self) -> None:
        with self.assertRaisesRegex(SystemExit, "only for push events.*dev branch"):
            self.run_export(ref="main")

    def test_rejects_non_push_event(self) -> None:
        with self.assertRaisesRegex(SystemExit, "only for push events.*dev branch"):
            self.run_export(event="workflow_dispatch")

    def test_rejects_another_repository(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot be built"):
            self.run_export(repository="IDUclub/another")


if __name__ == "__main__":
    unittest.main()
