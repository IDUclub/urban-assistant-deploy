from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "update-image.py"
SPEC = importlib.util.spec_from_file_location("update_image", SCRIPT)
assert SPEC and SPEC.loader
UPDATE_IMAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE_IMAGE)


class UpdateImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        overlay = self.root / "environments/dev/apps/example"
        overlay.mkdir(parents=True)
        self.catalog = {
            "environment": "dev",
            "services": [{
                "name": "example",
                "source": {"repository": "IDUclub/example", "branch": "dev"},
                "argocdPath": "environments/dev/apps/example",
                "atomicImages": ["api", "migrator"],
                "images": [
                    {"alias": "api", "kustomizeName": "logical/api", "repository": "registry.local/api"},
                    {"alias": "migrator", "kustomizeName": "logical/migrator", "repository": "registry.local/migrator"},
                ],
            }],
        }
        (overlay / "kustomization.yaml").write_text(
            yaml.safe_dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["base"],
                "images": [
                    {"name": "logical/api", "newName": "registry.local/api", "digest": "sha256:" + "0" * 64},
                    {"name": "logical/migrator", "newName": "registry.local/migrator", "digest": "sha256:" + "0" * 64},
                ],
            }, sort_keys=False),
            encoding="utf-8",
        )
        self.payload = {
            "service": "example",
            "environment": "dev",
            "source_repository": "IDUclub/example",
            "source_sha": "a" * 40,
            "images": [
                {"alias": "api", "repository": "registry.local/api", "digest": "sha256:" + "1" * 64},
                {"alias": "migrator", "repository": "registry.local/migrator", "digest": "sha256:" + "2" * 64},
            ],
            "workflow_run_url": "https://github.com/IDUclub/example/actions/runs/123",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_updates_all_release_digests_and_revision(self) -> None:
        service, images = UPDATE_IMAGE.validate_payload(self.payload, self.catalog)
        path = UPDATE_IMAGE.update_overlay(self.root, service, images, self.payload)
        result = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(result["images"][0]["digest"], "sha256:" + "1" * 64)
        self.assertEqual(result["images"][1]["digest"], "sha256:" + "2" * 64)
        self.assertEqual(result["commonAnnotations"]["deployment.urban-assistant/source-revision"], "a" * 40)

    def test_rejects_partial_atomic_release(self) -> None:
        self.payload["images"].pop()
        with self.assertRaisesRegex(UPDATE_IMAGE.PromotionError, "exactly these aliases"):
            UPDATE_IMAGE.validate_payload(self.payload, self.catalog)

    def test_rejects_repository_substitution(self) -> None:
        self.payload["images"][0]["repository"] = "attacker.invalid/api"
        with self.assertRaisesRegex(UPDATE_IMAGE.PromotionError, "not allowlisted"):
            UPDATE_IMAGE.validate_payload(self.payload, self.catalog)


if __name__ == "__main__":
    unittest.main()
