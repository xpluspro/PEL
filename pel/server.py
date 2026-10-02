"""Loopback-only inspection UI; each request owns its SQLite connection."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import normalize
from .engine import ExperienceEngine, terms
from .models import TaskContext
from .repository import SQLiteRepository

WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_BODY = 4 * 1024 * 1024


def make_server(db_path: str, port: int = 8765) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    with SQLiteRepository(db_path):
        pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status: int, content: bytes | dict | list, content_type: str = "application/json; charset=utf-8"):
            data = json.dumps(content, ensure_ascii=False).encode("utf-8") if isinstance(content, (dict, list)) else content
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self) -> bool:
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "Loopback Host required"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                self.send(403, {"error": "Same-origin requests required"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            parsed = urlparse(self.path)
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
            if parsed.path in static:
                filename, mime = static[parsed.path]
                self.send(200, (WEB_ROOT / filename).read_bytes(), mime)
                return
            try:
                with SQLiteRepository(db_path) as repo:
                    engine = ExperienceEngine(repo)
                    query = parse_qs(parsed.query)
                    if parsed.path == "/api/overview":
                        self.send(200, {**engine.overview(), "audit": repo.verify_history(), "default_project": os.getcwd(), "extractor": "offline_rules", "demo_episodes": sum(ep["source_uri"].startswith("demo:") for ep in repo.episodes())})
                    elif parsed.path == "/api/experiences":
                        items = [e.to_dict() for e in repo.experiences()]
                        for field, param in (("type", "type"), ("lifecycle_state", "status"), ("project", "project")):
                            value = query.get(param, [""])[0]
                            if value:
                                items = [item for item in items if item[field] == value]
                        search = query.get("q", [""])[0].casefold()
                        if search:
                            items = [item for item in items if search in item["statement"].casefold() or terms(search) & terms(item["statement"])]
                        self.send(200, items)
                    elif parsed.path.startswith("/api/experiences/"):
                        self.send(200, engine.inspect(parsed.path.rsplit("/", 1)[-1]))
                    elif parsed.path == "/api/episodes":
                        self.send(200, [{k: v for k, v in episode.items() if k != "events"} | {"event_count": len(episode["events"])} for episode in repo.episodes()])
                    elif parsed.path == "/api/compilations":
                        self.send(200, repo.compilations())
                    elif parsed.path == "/api/export":
                        self.send(200, repo.export())
                    else:
                        self.send(404, {"error": "Not found"})
            except KeyError as exc:
                self.send(404, {"error": str(exc)})
            except (OSError, ValueError) as exc:
                self.send(400, {"error": str(exc)})

        def do_POST(self):
            if not self.allowed():
                return
            if self.headers.get("X-PEL-Request") != "1":
                self.send(403, {"error": "X-PEL-Request: 1 required"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self.send(415, {"error": "JSON required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    self.send(413, {"error": "Body must be 1 byte to 4 MiB"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                with SQLiteRepository(db_path) as repo:
                    engine = ExperienceEngine(repo)
                    if self.path == "/api/compile":
                        allowed = {"task", "project", "domain", "agent", "task_id", "phase", "budget_tokens"}
                        if "task" not in payload or "project" not in payload:
                            raise ValueError("task and project are required")
                        context = TaskContext(**{k: v for k, v in payload.items() if k in allowed})
                        self.send(200, engine.compile(context, payload.get("format", "brief")))
                    elif self.path == "/api/feedback":
                        if not all(isinstance(payload.get(k), str) for k in ("id", "action", "reason")):
                            raise ValueError("id, action and reason must be strings")
                        result = engine.feedback(payload["id"], payload["action"], reason=payload["reason"], statement=payload.get("statement", ""), scope=payload.get("scope", ""), scope_key=payload.get("scope_key", ""))
                        self.send(200, result.to_dict())
                    elif self.path == "/api/ingest":
                        if not isinstance(payload.get("content"), str):
                            raise ValueError("content must be JSON/JSONL text")
                        episode = normalize(payload["content"], source=payload.get("source", "auto"), source_uri="upload:" + str(payload.get("filename", "session")), project=payload.get("project") or None, domain=payload.get("domain", ""))
                        self.send(200, engine.ingest(episode))
                    elif self.path == "/api/demo":
                        from .demo import demo
                        self.send(200, demo(engine))
                    else:
                        self.send(404, {"error": "Not found"})
            except KeyError as exc:
                self.send(404, {"error": str(exc)})
            except (OSError, ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(db_path: str, port: int = 8765, open_browser: bool = False):
    server = make_server(db_path, port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"PEL is running at {url}", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()

