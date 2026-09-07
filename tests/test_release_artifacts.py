from fnmatch import fnmatchcase
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).parents[1]


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/reusable-application-release.yaml").read_text(encoding="utf-8")
        )
        upload = next(
            step for step in workflow["jobs"]["build"]["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact@")
        )
        download = next(
            step for step in workflow["jobs"]["dispatch"]["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        )
        self.name_template = upload["with"]["name"]
        self.pattern_template = download["with"]["pattern"]
        # A download name would override the service-specific pattern.
        self.assertNotIn("name", download["with"])
        self.services = yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))["services"]
        self.artifacts = [
            (
                self.name_template.replace("${{ inputs.service }}", service["name"])
                .replace("${{ matrix.alias }}", image["alias"]),
                service["name"],
                image["alias"],
            )
            for service in self.services
            for image in service["images"]
        ]

    def test_artifact_names_are_unique_across_services_with_shared_aliases(self) -> None:
        names = [name for name, _, _ in self.artifacts]
        self.assertEqual(len(names), len(set(names)))

    def test_dispatch_downloads_only_its_service_images(self) -> None:
        # Simulate every service uploading into the same caller workflow run.
        # In particular, urban-api must select api+migrator, never urban-mcp's mcp.
        for service in self.services:
            with self.subTest(service=service["name"]):
                pattern = self.pattern_template.replace("${{ inputs.service }}", service["name"])
                selected = [
                    (owner, alias) for name, owner, alias in self.artifacts
                    if fnmatchcase(name, pattern)
                ]
                expected = [(service["name"], alias) for alias in service["atomicImages"]]
                self.assertCountEqual(selected, expected)


if __name__ == "__main__":
    unittest.main()
