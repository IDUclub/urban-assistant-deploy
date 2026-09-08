import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

import yaml


ROOT = Path(__file__).parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


PROBE = module("smoke_probe", "smoke-probe.py")
GENERATOR = module("generate_smoke", "generate-smoke-tests.py")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/healthy")
            self.end_headers()
            return
        self.send_response(503 if self.path == "/failed" else 200)
        self.end_headers()
        if self.path != "/empty":
            self.wfile.write(b'{"status":"ready"}')

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["method"] == "initialize"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if self.path == "/sse" else "application/json")
        self.send_header("Mcp-Session-Id", "smoke-session")
        self.end_headers()
        body = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}}
        if self.path == "/rpc-error":
            body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "failed"}}
        if self.path == "/array":
            body = []
        data = json.dumps(body).encode()
        self.wfile.write(b"event: message\ndata: " + data + b"\n\n" if self.path == "/sse" else data)

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_http_and_json(self):
        PROBE.check({"url": self.base + "/healthy", "jsonEquals": {"status": "ready"}})

    def test_rejects_bad_http_and_body(self):
        for path in ["/failed", "/redirect", "/empty"]:
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                PROBE.check({"url": self.base + path})
        for extra in [{"jsonEquals": {"status": "wrong"}}, {"contains": "swagger-ui"}]:
            with self.assertRaises(ValueError):
                PROBE.check({"url": self.base + "/healthy", **extra})

    def test_mcp_json_and_sse(self):
        for path in ["/json", "/sse"]:
            PROBE.check({"type": "mcp", "url": self.base + path})

    def test_rejects_rpc_error_and_invalid_json_shape(self):
        for path in ["/rpc-error", "/array"]:
            with self.assertRaises(ValueError):
                PROBE.check({"type": "mcp", "url": self.base + path})

    def test_failure_exits_nonzero_without_waiting_in_test(self):
        with patch.dict("os.environ", {"SMOKE_CHECKS": json.dumps([{"url": self.base + "/failed"}])}), \
                patch.object(PROBE.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as error:
            PROBE.main()
        self.assertNotEqual(error.exception.code, 0)


class ManifestTests(unittest.TestCase):
    def test_all_services_have_safe_jobs_and_real_internal_service_ports(self):
        catalog = yaml.safe_load((ROOT / "services.yaml").read_text())
        for service in catalog["services"]:
            with self.subTest(service=service["name"]):
                base = ROOT / "apps" / service["name"] / "base"
                kustomization = yaml.safe_load((base / "kustomization.yaml").read_text())
                self.assertIn("post-sync-smoke-test.yaml", kustomization["resources"])
                generated = GENERATOR.job(ROOT, service)
                actual = yaml.safe_load((base / "post-sync-smoke-test.yaml").read_text())
                self.assertEqual(actual, generated)
                self.assertEqual(actual["metadata"]["annotations"]["argocd.argoproj.io/hook"], "PostSync")
                self.assertEqual(actual["spec"]["backoffLimit"], 0)
                self.assertLessEqual(actual["spec"]["activeDeadlineSeconds"], 300)
                pod = actual["spec"]["template"]
                self.assertFalse(pod["spec"]["automountServiceAccountToken"])
                self.assertNotIn("envFrom", pod["spec"]["containers"][0])
                objects = [d for p in base.glob("*.yaml") for d in yaml.safe_load_all(p.read_text()) if isinstance(d, dict)]
                services = {d["metadata"]["name"]: d for d in objects if d.get("kind") == "Service"}
                for probe in service["smokeChecks"]:
                    url = urlsplit(probe["url"])
                    self.assertEqual(url.scheme, "http")
                    self.assertIn(url.hostname, services)
                    self.assertIn(url.port, [p["port"] for p in services[url.hostname]["spec"]["ports"]])
                # A smoke Pod must never become a backend of the application's Service.
                for svc in services.values():
                    selector = svc["spec"].get("selector", {})
                    if selector:
                        self.assertFalse(all(pod["metadata"]["labels"].get(k) == v for k, v in selector.items()))

    def test_application_sync_is_not_selective_and_has_status_subscription(self):
        appset = yaml.safe_load((ROOT / "argocd/root/applicationset-apps.yaml").read_text())
        template = appset["spec"]["template"]
        self.assertNotIn("ApplyOutOfSyncOnly=true", template["spec"]["syncPolicy"]["syncOptions"])
        self.assertIn("notifications.argoproj.io/subscribe.ua-github-status.github", template["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
