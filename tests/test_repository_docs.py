"""Keep the developer guide's links and delivery inventory in sync with Git."""

from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]


class RepositoryDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", cls.readme, flags=re.M | re.S)
        cls.links = re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", cls.prose)
        cls.catalog = yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))

    def test_local_links_and_explicit_anchors_exist(self) -> None:
        local_links = [link for link in self.links if not urlsplit(link).scheme]
        self.assertTrue(local_links, "README should link to the files developers edit")
        for link in local_links:
            with self.subTest(link=link):
                parsed = urlsplit(link)
                target = (ROOT / unquote(parsed.path)).resolve() if parsed.path else ROOT / "README.md"
                self.assertTrue(target == ROOT or ROOT in target.parents, "link escapes repository")
                self.assertTrue(target.exists(), f"missing link target: {link}")
                if parsed.fragment:
                    anchors = re.findall(r'<a id="([^"]+)"', target.read_text(encoding="utf-8"))
                    self.assertIn(unquote(parsed.fragment), anchors, f"missing anchor: {link}")

    def test_service_table_matches_source_repositories_and_atomic_images(self) -> None:
        rows = re.findall(
            r"^\| `([^`]+)` \| \[([^\]]+)\]\(https://github\.com/([^\)]+)\) \| ([^|]+) \|",
            self.prose,
            flags=re.M,
        )
        self.assertEqual(len(rows), len(self.catalog["services"]))
        documented = {
            name: (label, repository, re.findall(r"`([^`]+)`", images))
            for name, label, repository, images in rows
        }
        expected = {
            service["name"]: (
                service["source"]["repository"], service["source"]["repository"], service["atomicImages"]
            )
            for service in self.catalog["services"]
        }
        self.assertEqual(documented, expected)

    def test_all_workflows_are_linked(self) -> None:
        expected = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / ".github/workflows").iterdir()
            if path.suffix in {".yaml", ".yml"}
        }
        documented = {
            link for link in self.links
            if link.startswith(".github/workflows/") and Path(link).suffix in {".yaml", ".yml"}
        }
        self.assertEqual(documented, expected)


if __name__ == "__main__":
    unittest.main()
