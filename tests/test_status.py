"""Problems per file (own checks + Core) and where replies go."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from test_apply_webhooks import AUTH
from test_connectors import load_app


class FakeCore(BaseHTTPRequestHandler):
    load_errors = True  # False: an older Core without /api/admin/load-errors

    def do_GET(self):
        if self.path == "/api/handlers":
            body = [{"id": "worker", "direction": "inbound", "state": "degraded", "match": {}},
                    {"id": "echo", "direction": "inbound", "state": "healthy", "match": {}},
                    {"id": "pushover", "direction": "outbound", "state": "healthy", "dynamic": True,
                     "match": {"source": ["webhook:voice"]}}]
        elif self.path == "/api/admin/load-errors" and FakeCore.load_errors:
            body = {"errors": ["61.0-skipped.json: 'type' is a required property",
                               "99.0-echo.json: was broken at the last load"]}
        elif self.path == "/api/plugin/channels":
            body = [{"channel_id": "cursor:obsidian", "triggers": ["@obsidian"]}]
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def core_url():
    FakeCore.load_errors = True
    srv = HTTPServer(("127.0.0.1", 0), FakeCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _w(d, rel, data):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))


@pytest.fixture
def cfg(monkeypatch, tmp_path, core_url):
    (tmp_path / "core.yaml").write_text("port: 8080\nwebhooks:\n  voice: t\n  pebble: t2\n")
    base = {"direction": "inbound", "type": "exec_json", "exec": "x"}
    _w(tmp_path, "handlers/10.0-worker.json", {**base, "id": "worker"})
    _w(tmp_path, "handlers/60.0-broken.json", "{nope")
    _w(tmp_path, "handlers/61.0-skipped.json", {"id": "skipped", "direction": "inbound"})
    _w(tmp_path, "handlers/70.0-badregex.json", {
        **base, "id": "badregex", "exec": "{sijba_plugins}/regex-router/main.py",
        "env": {"ROUTES": json.dumps([{"pattern": "^(", "inject": "@a {{ text }}"}])}})
    _w(tmp_path, "handlers/71.0-nofile.json", {
        **base, "id": "nofile", "exec": "{sijba_plugins}/regex-router/main.py",
        "env": {"ROUTES_FILE": "/data/config/routes/missing.json"}})
    _w(tmp_path, "handlers/80.0-reply-web.json", {**base, "id": "reply-web", "direction": "outbound",
                                                 "match": {"source": ["telegram"]}})
    _w(tmp_path, "handlers/99.0-echo.json", {**base, "id": "echo"})
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL=core_url)
    return TestClient(m.app)


def test_problems_per_file(cfg):
    r = cfg.get("/api/status", headers=AUTH).json()
    assert r["core_error"] is None
    files = {path: [(p["source"], p["message"]) for p in probs] for path, probs in r["files"].items()}
    assert files["handlers/60.0-broken.json"][0][0] == "check"
    assert [p[0] for p in files["handlers/61.0-skipped.json"]] == ["check"]  # not twice
    assert files["handlers/99.0-echo.json"] == [("sijba", "was broken at the last load")]
    assert files["handlers/10.0-worker.json"] == [("running", "sijba reports this handler as degraded")]
    assert "invalid regex" in files["handlers/70.0-badregex.json"][0][1]
    assert "cannot read routes/missing.json" in files["handlers/71.0-nofile.json"][0][1]


def test_older_core_without_load_errors(cfg):
    FakeCore.load_errors = False
    files = cfg.get("/api/status", headers=AUTH).json()["files"]
    assert all(p["source"] != "sijba" for probs in files.values() for p in probs)
    assert "handlers/60.0-broken.json" in files


def test_core_down_still_shows_own_checks(monkeypatch, tmp_path):
    _w(tmp_path, "handlers/60.0-broken.json", "{nope")
    m = load_app(monkeypatch, tmp_path, SIJBA_CORE_URL="http://127.0.0.1:1")
    r = TestClient(m.app).get("/api/status").json()
    assert r["core_error"] and "handlers/60.0-broken.json" in r["files"]


def test_routing_overview_has_replies_and_status(cfg):
    r = cfg.get("/api/routing", headers=AUTH).json()
    assert {(x["id"], x["connector"], tuple(x["sources"])) for x in r["replies"]} == {
        ("reply-web", False, ("telegram",)), ("pushover", True, ("webhook:voice",))}
    assert r["self_delivering"] == ["web", "app"]
    assert "handlers/61.0-skipped.json" in r["status"]
    assert [s["id"] for s in r["steps"]] == ["worker", "skipped", "badregex", "nofile", "echo"]
    assert [t["mention"] for t in r["targets"]] == ["@obsidian", "@a"]
