import concurrent.futures
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pel.adapters import normalize
from pel.engine import ExperienceEngine
from pel.repository import SQLiteRepository
from pel.server import make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = str(Path(self.directory.name) / "pel.sqlite3")
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.project = self.directory.name

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, payload=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        default = {"Content-Type":"application/json", "X-PEL-Request":"1"}
        client.request(method, path, body, {**default, **(headers or {})})
        response = client.getresponse()
        status, data, response_headers = response.status, response.read(), dict(response.getheaders())
        client.close()
        if "application/json" in response_headers.get("Content-Type", ""):
            data = json.loads(data)
        return status, data, response_headers

    def test_loopback_binding_and_static_content_security(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, data, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Personal Experience Layer", data)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/../../PRD.txt")[0], 404)

    def test_origin_host_and_request_header_guards(self):
        for headers in ({"Origin":"https://example.org"}, {"Host":"example.org"}, {"X-PEL-Request":""}):
            self.assertEqual(self.request("POST", "/api/demo", {}, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", {}, {"Content-Type":"text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/api/export", headers={"Host":"attacker.example"})[0], 403)

    def test_import_compile_feedback_and_export_round_trip(self):
        episode = {"session_id":"web", "project":self.project, "objective":"Optimize ragged reduction", "events":[{"role":"assistant","text":"Failure: Ragged reduction tile=256 regressed latency."}]}
        status, result, _ = self.request("POST", "/api/ingest", {"content":json.dumps(episode), "source":"episode"})
        self.assertEqual(status, 200)
        self.assertEqual(result["created"], 1)
        items = self.request("GET", "/api/experiences")[1]
        experience_id = items[0]["id"]
        status, brief, _ = self.request("POST", "/api/compile", {"task":"Optimize ragged reduction", "project":self.project})
        self.assertEqual(status, 200)
        self.assertIn(experience_id, brief["selected_ids"])
        status, updated, _ = self.request("POST", "/api/feedback", {"id":experience_id,"action":"strengthen","reason":"Reproduced the failure in a benchmark."})
        self.assertEqual(status, 200)
        self.assertGreater(updated["confidence"], items[0]["confidence"])
        detail = self.request("GET", "/api/experiences/"+experience_id)[1]
        self.assertEqual(len(detail["evidence"]), 2)
        snapshot = self.request("GET", "/api/export")[1]
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertTrue(self.request("GET", "/api/overview")[1]["audit"]["valid"])

    def test_bad_payloads_return_actionable_json_errors(self):
        for path, payload in (("/api/compile",{}),("/api/compile",{"task":"test","project":self.project,"budget_tokens":[]}),
                              ("/api/ingest",{"content":"not json"}),("/api/feedback",{})):
            status, result, _ = self.request("POST",path,payload)
            self.assertEqual(status,400)
            self.assertIn("error",result)
        self.assertEqual(self.request("GET","/api/experiences/missing")[0],404)

    def test_concurrent_imports_do_not_duplicate_or_break_audit(self):
        content = json.dumps({"session_id":"concurrent", "project":self.project, "objective":"Kernel latency", "events":[{"role":"assistant","text":"Failure: Increasing tile size regressed the kernel latency."}]})
        def ingest(_):
            with SQLiteRepository(self.db) as repository:
                return ExperienceEngine(repository).ingest(normalize(content))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(ingest, range(6)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        with SQLiteRepository(self.db) as repository:
            self.assertEqual(len(repository.experiences()), 1)
            self.assertTrue(repository.verify_history()["valid"])

