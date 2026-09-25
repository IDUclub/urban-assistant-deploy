from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).parents[1]


class FrontendContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))
        self.env = dict(
            line.split("=", 1)
            for line in (ROOT / "environments/dev/build/frontend.env").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
        documents = yaml.safe_load_all((ROOT / "platform/gateway/routes.yaml").read_text(encoding="utf-8"))
        route = next(document for document in documents if document["metadata"]["name"] == "urban-assistant-https")
        self.rules = {
            match["path"]["value"]: rule
            for rule in route["spec"]["rules"]
            for match in rule["matches"]
        }

    def test_frontend_uses_transferred_repository_and_dev_branch(self) -> None:
        frontend = next(service for service in self.catalog["services"] if service["name"] == "frontend")
        self.assertEqual(frontend["source"], {"repository": "IDUclub/Urban-Assistant-Client", "branch": "dev"})

    def test_frontend_api_variables_have_explicit_gateway_routes(self) -> None:
        expected = {
            "VITE_URBAN_API": "/urban-api",
            "VITE_LLM_API": "/gmart",
            "VITE_LLM_RESTRICTIONS_API": "/gmart",
            "VITE_LLM_CHAT_HISTORY_API": "/chat-storage",
            "VITE_PZZ_COMPARE_API": "/pzz-compare",
            "VITE_GENBUILDER_API": "/genbuilder",
            "VITE_GENPLANNER_API": "/genplanner",
            "VITE_DOCUMENTS_API": "/idu-dvd",
            "VITE_BUILDPLANNER_API": "/buildplanner",
        }
        for key, prefix in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.env.get(key), prefix)
                self.assertIn(prefix, self.rules)

    def test_new_routes_match_service_ports_and_backend_paths(self) -> None:
        expected = {
            "/genbuilder": ("genbuilder", 8000, "/"),
            "/genplanner": ("genplanner", 8080, None),
            "/idu-dvd": ("idu-dvd", 8000, "/"),
            "/buildplanner": ("buildplanner", 8080, None),
        }
        for prefix, (name, port, replacement) in expected.items():
            with self.subTest(prefix=prefix):
                rule = self.rules[prefix]
                self.assertEqual(rule["matches"], [{"path": {"type": "PathPrefix", "value": prefix}}])
                self.assertEqual(rule["backendRefs"], [
                    {"group": "", "kind": "Service", "name": name, "port": port, "weight": 1},
                ])
                filters = rule.get("filters", [])
                if replacement is None:
                    self.assertEqual(filters, [])
                else:
                    self.assertEqual(filters, [{
                        "type": "URLRewrite",
                        "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": replacement}},
                    }])
                services = [
                    document
                    for path in (ROOT / "apps" / name / "base").glob("*.yaml")
                    for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                    if document and document.get("kind") == "Service" and document["metadata"]["name"] == name
                ]
                self.assertEqual(len(services), 1)
                self.assertIn(port, [item["port"] for item in services[0]["spec"]["ports"]])

    def test_public_build_tokens_and_external_urls_stay_out_of_git_config(self) -> None:
        for key in ("VITE_MAPBOX_TOKEN", "VITE_KEYCLOAK_AUTH_URL", "VITE_KEYCLOAK_AUTH_LOGOUT_REDIRECT"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.env)
                self.assertIn(key, self.catalog["policies"]["githubSecretBuildKeys"])

    def test_synapse_server_settings_are_delivered_at_runtime(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "environments/dev/apps/frontend/vault-secret.yaml").read_text(encoding="utf-8")
        )["spec"]
        expected = {
            "SYNAPSE_EMAIL", "SYNAPSE_PASSWORD", "SYNAPSE_API_URL",
            "KEYCLOAK_AUTH_URL", "KEYCLOAK_AUTH_REALM",
        }
        templates = spec["destination"]["transformation"]["templates"]
        self.assertEqual(set(templates), expected)
        for name in expected:
            self.assertEqual(templates[name]["text"], '{{ get .Secrets "' + name.lower() + '" }}')
        deployment = next(yaml.safe_load_all(
            (ROOT / "apps/frontend/base/workload.yaml").read_text(encoding="utf-8")
        ))
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertIn({"secretRef": {"name": spec["destination"]["name"]}}, container["envFrom"])
        self.assertIn(
            {"kind": "Deployment", "name": deployment["metadata"]["name"]},
            spec["rolloutRestartTargets"],
        )
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/reusable-application-release.yaml").read_text(encoding="utf-8")
        )
        build_env = next(
            step["env"] for step in workflow["jobs"]["build"]["steps"]
            if step.get("name") == "Render frontend build environment"
        )
        renderer = (ROOT / "scripts/render-frontend-env.sh").read_text(encoding="utf-8")
        for name in ("SYNAPSE_EMAIL", "SYNAPSE_PASSWORD", "SYNAPSE_API_URL"):
            self.assertNotIn(name, build_env)
            self.assertNotIn(name, renderer)
            self.assertNotIn(name, self.env)
        self.assertEqual(build_env["SYNAPSE_WORKFLOW_ID"], "${{ secrets.SYNAPSE_WORKFLOW_ID }}")


if __name__ == "__main__":
    unittest.main()
