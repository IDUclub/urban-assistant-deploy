import copy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import yaml


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("forward_status", ROOT / "scripts/forward-deployment-status.py")
RELAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELAY)
BOT = "idu-argo-status[bot]"


def record(state="success", identifier=20, stamp="2026-09-08T10:00:00Z", bot=BOT):
    return {"id": identifier, "state": state, "created_at": stamp,
            "creator": {"login": bot}, "target_url": "https://argocd.example.invalid/applications/argocd/dev-example"}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Status relay test")
        self.git("config", "user.email", "test@example.invalid")
        self.service = {"name": "example", "source": {"repository": "IDUclub/example", "branch": "dev"},
                        "argocdPath": "environments/dev/apps/example"}
        # Two independently released services may share a source repository/SHA.
        sibling = copy.deepcopy(self.service)
        sibling.update(name="example-mcp", argocdPath="environments/dev/apps/example-mcp")
        self.catalog = {"environment": "dev", "services": [self.service, sibling]}
        self.save("services.yaml", self.catalog)
        self.overlay = {"commonAnnotations": {
            "deployment.urban-assistant/source-revision": "a" * 40,
            "deployment.urban-assistant/source-workflow": "https://github.com/IDUclub/example/actions/runs/1",
        }}
        self.path = self.service["argocdPath"] + "/kustomization.yaml"
        self.save(self.path, self.overlay)
        self.save(sibling["argocdPath"] + "/kustomization.yaml", self.overlay)
        self.old_revision = self.commit("working release")
        self.overlay["commonAnnotations"]["deployment.urban-assistant/source-revision"] = "b" * 40
        self.save(self.path, self.overlay)
        self.new_revision = self.commit("new release")
        self.event = {"repository": {"full_name": RELAY.DEPLOY_REPOSITORY}, "context": "argocd/dev-example",
                      "sha": self.old_revision, "state": "pending", "sender": {"login": BOT, "type": "Bot"}}
        self.plan = RELAY.resolve(self.root, self.event)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def save(self, path, document):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(document), encoding="utf-8")

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def mirrored(self, plan=None, upstream=None):
        value = RELAY.status_payload(plan or self.plan, upstream or record(), BOT)
        value["creator"] = {"login": BOT}
        return value

    def test_reads_exact_event_commit_not_main_or_github_sha(self):
        self.assertEqual(self.plan["source_sha"], "a" * 40)
        self.assertEqual(self.plan["deploy_sha"], self.old_revision)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.new_revision)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_shared_repository_services_keep_distinct_contexts(self):
        other = RELAY.resolve(self.root, dict(self.event, context="argocd/dev-example-mcp"))
        self.assertEqual(other["source_repository"], self.plan["source_repository"])
        self.assertEqual(other["source_sha"], self.plan["source_sha"])
        self.assertNotEqual(other["context"], self.plan["context"])

    def test_rejects_bad_event_scope_context_sha_and_sender(self):
        for fields in [dict(repository={"full_name": "attacker/deploy"}), dict(context="argocd/dev-../../example"),
                       dict(context="argocd/dev-missing"), dict(sha="main"), dict(state="healthy"),
                       dict(sender={"login": "someone", "type": "User"})]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                RELAY.resolve(self.root, dict(self.event, **fields))

    def test_rejects_unmerged_revision(self):
        self.git("checkout", "-qb", "unmerged", self.old_revision)
        self.save("unmerged.yaml", {"data": "not trusted"})
        revision = self.commit("unmerged")
        self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(ValueError, "not merged"):
            RELAY.resolve(self.root, dict(self.event, sha=revision))

    def test_current_allowlist_blocks_repository_retargeting(self):
        self.service["source"]["repository"] = "IDUclub/renamed"
        self.save("services.yaml", self.catalog)
        with self.assertRaisesRegex(ValueError, "current allowlist"):
            RELAY.resolve(self.root, self.event)

    def test_skips_legacy_but_rejects_invalid_source_sha_or_workflow(self):
        for sha in ["legacy-import", None, "main", "b" * 40]:
            self.overlay["commonAnnotations"]["deployment.urban-assistant/source-revision"] = sha
            self.overlay["commonAnnotations"]["deployment.urban-assistant/source-workflow"] = "https://github.com/attacker/repo/actions/runs/1"
            self.save(self.path, self.overlay)
            revision = self.commit("test provenance " + str(sha))
            if sha in ("legacy-import", None):
                self.assertIsNone(RELAY.resolve(self.root, dict(self.event, sha=revision)))
            else:
                with self.assertRaises(ValueError):
                    RELAY.resolve(self.root, dict(self.event, sha=revision))

    def test_delayed_pending_forwards_current_success_from_api(self):
        client = Mock()
        client.latest.side_effect = [None, record("success")]
        message = RELAY.forward(self.root, self.event, self.plan, BOT, client)
        self.assertIn("Forwarded success", message)
        args = client.post.call_args.args
        self.assertEqual(args[:2], ("IDUclub/example", "a" * 40))
        self.assertEqual(args[2]["state"], "success")
        self.assertEqual(args[2]["context"], self.event["context"])
        self.assertIn(self.old_revision, args[2]["description"])
        self.assertEqual(args[2]["target_url"], record()["target_url"])

    def test_rejects_wrong_app_sender_before_any_api_call(self):
        client = Mock()
        event = dict(self.event, sender={"login": "another-app[bot]", "type": "Bot"})
        with self.assertRaisesRegex(ValueError, "event sender"):
            RELAY.forward(self.root, event, self.plan, BOT, client)
        client.latest.assert_not_called()
        client.post.assert_not_called()

    def test_rejects_spoofed_status_creator_or_missing_upstream(self):
        for latest in [None, record(bot="another-app[bot]")]:
            client = Mock()
            client.latest.side_effect = [None, latest]
            with self.subTest(latest=latest), self.assertRaisesRegex(ValueError, "configured Argo"):
                RELAY.forward(self.root, self.event, self.plan, BOT, client)
            client.post.assert_not_called()

    def test_status_payload_states_and_url_validation(self):
        for state in RELAY.STATES:
            value = RELAY.status_payload(self.plan, record(state), BOT)
            self.assertEqual(value["state"], state)
            self.assertLessEqual(len(value["description"]), 140)
        for fields in [dict(target_url="http://unsafe.invalid"), dict(target_url="https://user:password@example.invalid"),
                       dict(state="healthy"), dict(id=0), dict(created_at="not-a-time")]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                RELAY.status_payload(self.plan, dict(record(), **fields), BOT)

    def test_duplicate_is_not_posted(self):
        client = Mock()
        client.latest.side_effect = [self.mirrored(), record()]
        message = RELAY.forward(self.root, self.event, self.plan, BOT, client)
        self.assertIn("Skipped", message)
        client.post.assert_not_called()

    def test_old_deploy_does_not_overwrite_newer_deploy_on_same_source_sha(self):
        newer_plan = dict(self.plan, deploy_sha=self.new_revision)
        self.assertFalse(RELAY.should_publish(self.root, self.plan, record(), self.mirrored(newer_plan), BOT))

    def test_new_deploy_and_new_sync_attempt_may_replace_a_terminal_status(self):
        newer_plan = dict(self.plan, deploy_sha=self.new_revision)
        self.assertTrue(RELAY.should_publish(self.root, newer_plan, record("pending"), self.mirrored(), BOT))
        new_attempt = record("pending", identifier=30, stamp="2026-09-08T11:00:00Z")
        self.assertTrue(RELAY.should_publish(self.root, self.plan, new_attempt, self.mirrored(), BOT))
        old_attempt = record("pending", identifier=10, stamp="2026-09-08T09:00:00Z")
        self.assertFalse(RELAY.should_publish(self.root, self.plan, old_attempt, self.mirrored(), BOT))

    def test_unrecognized_or_foreign_source_status_is_not_overwritten(self):
        for previous in [dict(self.mirrored(), description="not a relay status"),
                         dict(self.mirrored(), creator={"login": "another-app[bot]"})]:
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                RELAY.should_publish(self.root, self.plan, record(), previous, BOT)

    def test_rollback_resolves_restored_source_sha_from_new_deploy_commit(self):
        self.overlay["commonAnnotations"]["deployment.urban-assistant/source-revision"] = "a" * 40
        self.save(self.path, self.overlay)
        rollback_revision = self.commit("manual rollback")
        plan = RELAY.resolve(self.root, dict(self.event, sha=rollback_revision))
        self.assertEqual(plan["source_sha"], "a" * 40)
        self.assertEqual(plan["deploy_sha"], rollback_revision)
        self.assertTrue(RELAY.should_publish(self.root, plan, record(), self.mirrored(), BOT))


class ApiAndWorkflowTests(unittest.TestCase):
    def test_status_pagination_selects_first_matching_context(self):
        client = RELAY.GitHub("unused", "unused")
        expected = dict(record(), context="argocd/dev-example")
        client.request = Mock(side_effect=[[{"context": "unrelated"}] * 100,
                                         [expected, dict(record(identifier=10), context="argocd/dev-example")]])
        self.assertEqual(client.latest("IDUclub/example", "a" * 40, "argocd/dev-example", "source"), expected)
        self.assertIn("page=2", client.request.call_args.args[1])

    def test_workflow_uses_trusted_code_scoped_token_and_per_source_lock(self):
        document = yaml.safe_load((ROOT / ".github/workflows/forward-deployment-status.yml").read_text())
        self.assertIn("status", document.get("on", document.get(True)))
        forward = document["jobs"]["forward"]
        self.assertEqual(forward["permissions"], {"contents": "read", "statuses": "read"})
        self.assertIn("needs.resolve.outputs.service", forward["concurrency"]["group"])
        self.assertIn("needs.resolve.outputs.source-sha", forward["concurrency"]["group"])
        self.assertFalse(forward["concurrency"]["cancel-in-progress"])
        for job in document["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["ref"], "main")
                    self.assertEqual(step["with"]["fetch-depth"], 0)
                    self.assertFalse(step["with"]["persist-credentials"])
                if step.get("uses", "").startswith("actions/create-github-app-token@"):
                    self.assertEqual(step["with"]["permission-statuses"], "write")
                    self.assertEqual(step["with"]["repositories"], "${{ needs.resolve.outputs.source-name }}")

    def test_registry_auth_is_absent_from_all_workflows_and_digest_helper(self):
        paths = list((ROOT / ".github/workflows").glob("*.y*ml")) + [ROOT / "scripts/verify-registry-digests.sh"]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertNotRegex(text, r"REGISTRY_(?:READ_)?(?:USERNAME|PASSWORD)")
                self.assertNotIn("docker/login-action", text)


if __name__ == "__main__":
    unittest.main()
