"""Apply (Core reload pass-through), login check and webhook management."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml
from fastapi.testclient import TestClient

from test_connectors import load_app

AUTH = {"Authorization": "Bearer ui-tok"}


class FakeCore(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):
        FakeCore.calls.append((self.path, self.headers.get("Authorization")))
        raw = json.dumps({"handlers": ["echo"], "restart_required": [], "errors": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def core_server():
    FakeCore.calls = []
    srv = HTTPServer(("127.0.0.1", 0), FakeCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def client(monkeypatch, tmp_path, core_server):
    (tmp_path / "core.yaml").write_text("port: 8080\nwebhooks:\n  voice: tok-voice\n  env: ${HOOK}\n")
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL=core_server)
    return TestClient(m.app), tmp_path


def test_me_checks_token(client):
    c, _ = client
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers=AUTH).json() == {"ok": True, "connectors": False}


def test_apply_calls_core_reload_with_token(client):
    c, _ = client
    r = c.post("/api/apply", headers=AUTH)
    assert r.status_code == 200 and r.json()["handlers"] == ["echo"]
    assert FakeCore.calls == [("/api/admin/reload", "Bearer ui-tok")]


def test_apply_core_down_is_502(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path, SIJBA_CORE_URL="http://127.0.0.1:1")
    r = TestClient(m.app).post("/api/apply")
    assert r.status_code == 502 and "unreachable" in r.json()["error"]


def test_webhooks_list_add_delete(client):
    c, d = client
    hooks = c.get("/api/webhooks", headers=AUTH).json()["webhooks"]
    assert {"name": "voice", "token": "tok-voice", "from_env": False} in hooks
    assert {"name": "env", "token": "${HOOK}", "from_env": True} in hooks

    r = c.post("/api/webhooks", json={"name": "Pebble"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["name"] == "pebble" and len(r.json()["token"]) > 20
    saved = yaml.safe_load((d / "core.yaml").read_text())
    assert saved["webhooks"]["pebble"] == r.json()["token"] and saved["port"] == 8080

    assert c.post("/api/webhooks", json={"name": "pebble"}, headers=AUTH).status_code == 409
    assert c.post("/api/webhooks", json={"name": "bad name!"}, headers=AUTH).status_code == 422

    assert c.delete("/api/webhooks/pebble", headers=AUTH).status_code == 200
    assert "pebble" not in yaml.safe_load((d / "core.yaml").read_text())["webhooks"]
    assert c.delete("/api/webhooks/pebble", headers=AUTH).status_code == 404
