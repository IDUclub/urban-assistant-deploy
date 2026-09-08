from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("rollback", ROOT / "scripts/rollback-image.py")
ROLLBACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROLLBACK)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Rollback test")
        self.git("config", "user.email", "test@example.invalid")
        self.service = {
            "name": "example", "source": {"repository": "IDUclub/example", "branch": "dev"},
            "argocdPath": "environments/dev/apps/example", "atomicImages": ["api", "migrator"],
            "images": [{"alias": alias, "kustomizeName": "logical/" + alias,
                        "repository": "registry.local/" + alias} for alias in ["api", "migrator"]],
        }
        self.save("services.yaml", {"environment": "dev", "services": [self.service]})
        self.overlay = self.service["argocdPath"] + "/kustomization.yaml"
        self.old = {
            "resources": ["old-base"], "patches": [{"path": "old-patch.yaml"}],
            "commonAnnotations": {"deployment.urban-assistant/source-revision": "a" * 40,
                                  "deployment.urban-assistant/source-workflow": "https://github.com/IDUclub/example/actions/runs/1"},
            "images": [{"name": "logical/" + alias, "newName": "registry.local/" + alias,
                        "digest": "sha256:" + digit * 64} for alias, digit in [("api", "1"), ("migrator", "2")]],
        }
        self.save(self.overlay, self.old)
        self.git("add", ".")
        self.git("commit", "-qm", "working release")
        self.revision = self.git("rev-parse", "HEAD")
        self.current = json.loads(json.dumps(self.old))
        self.current["resources"] = ["new-base", "configmap.yaml"]
        self.current["patches"] = [{"path": "new-patch.yaml"}]
        self.current["commonAnnotations"]["deployment.urban-assistant/source-revision"] = "b" * 40
        self.current["commonAnnotations"]["unrelated"] = "keep"
        for entry in self.current["images"]:
            entry["digest"] = "sha256:" + "3" * 64
        self.save(self.overlay, self.current)
        self.save("other-service.yaml", {"data": "unchanged"})
        self.git("add", ".")
        self.git("commit", "-qm", "new release and config")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def save(self, path, document):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_restores_atomic_images_without_reverting_configuration(self):
        result = ROLLBACK.prepare(self.root, "example", self.revision, "Failed smoke test")
        document = yaml.safe_load((self.root / self.overlay).read_text())
        self.assertEqual(document["images"], self.old["images"])
        self.assertEqual(document["resources"], self.current["resources"])
        self.assertEqual(document["patches"], self.current["patches"])
        self.assertEqual(document["commonAnnotations"]["unrelated"], "keep")
        self.assertEqual(result["payload"]["source_sha"], "a" * 40)
        self.assertEqual(self.git("diff", "--name-only"), self.overlay)

    def test_rejects_non_sha_unknown_service_and_multiline_reason(self):
        for service, revision, reason in [("example", "main~1", "reason"), ("missing", self.revision, "reason"),
                                         ("example", self.revision, "line\nbreak")]:
            with self.subTest(service=service, revision=revision, reason=reason):
                with self.assertRaises(ValueError):
                    ROLLBACK.prepare(self.root, service, revision, reason)
        self.assertEqual(self.git("diff", "--name-only"), "")

    def test_rejects_images_outside_current_catalog(self):
        self.service["images"][0]["repository"] = "registry.local/renamed"
        self.save("services.yaml", {"environment": "dev", "services": [self.service]})
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            ROLLBACK.prepare(self.root, "example", self.revision, "reason")
        self.assertEqual(yaml.safe_load((self.root / self.overlay).read_text()), self.current)

    def test_rejects_noop(self):
        with self.assertRaisesRegex(ValueError, "nothing to roll back"):
            ROLLBACK.prepare(self.root, "example", self.git("rev-parse", "HEAD"), "reason")

    def test_rejects_commit_not_merged_into_main(self):
        self.git("checkout", "-qb", "unmerged", self.revision)
        self.save("unmerged.yaml", {"value": 1})
        self.git("add", ".")
        self.git("commit", "-qm", "unmerged change")
        revision = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        with self.assertRaises(ValueError):
            ROLLBACK.prepare(self.root, "example", revision, "reason")

    def test_workflow_is_manual_and_does_not_enable_auto_merge(self):
        workflow = (ROOT / ".github/workflows/rollback-dev.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertNotIn("enable-pull-request-automerge", workflow)
        self.assertIn("add-paths: ${{ steps.rollback.outputs.overlay }}", workflow)
        config = yaml.safe_load(workflow)
        inputs = (config.get("on") or config[True])["workflow_dispatch"]["inputs"]
        catalog = yaml.safe_load((ROOT / "services.yaml").read_text())
        self.assertEqual(set(inputs["service"]["options"]), {s["name"] for s in catalog["services"]})


if __name__ == "__main__":
    unittest.main()
