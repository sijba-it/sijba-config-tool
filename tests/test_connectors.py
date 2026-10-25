"""Connector endpoints: a pure pass-through to the provisioner API."""
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient


class FakeProvisioner(BaseHTTPRequestHandler):
    calls: list = []

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        FakeProvisioner.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer prov-tok":
            return self._reply(401, {"error": "unauthorized"})
        if self.path == "/connectors/catalog":
            return self._reply(200, {"catalog": [{"type": "pushover", "label": "Pushover", "fields": []}]})
        if self.path == "/connectors":
            return self._reply(200, {"connectors": []})
        if self.path == "/connectors/boom":
            return self._reply(422, {"error": "PUSHOVER_USER_KEY is required"})
        return self._reply(200, {"type": "pushover", "echo": body})

    do_GET = do_PUT = do_DELETE = _handle

    def log_message(self, *a):
        pass


@pytest.fixture
def prov_server():
    FakeProvisioner.calls = []
    srv = HTTPServer(("127.0.0.1", 0), FakeProvisioner)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def load_app(monkeypatch, tmp_path, **env):
    for k in ("PROVISIONER_URL", "PROVISIONER_TOKEN", "SIJBA_AUTH_TOKEN", "ROOT_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIJBA_CONFIG_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main
    return importlib.reload(main)


def test_without_provisioner_connectors_are_off(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path)
    c = TestClient(m.app)
    assert c.get("/api/connectors").status_code == 404
    html = c.get("/").text
    assert 'const connectorsEnabled = "0" === "1"' in html


def test_with_provisioner_ui_enabled_and_proxied(monkeypatch, tmp_path, prov_server):
    m = load_app(monkeypatch, tmp_path, PROVISIONER_URL=prov_server, PROVISIONER_TOKEN="prov-tok")
    c = TestClient(m.app)
    assert 'const connectorsEnabled = "1" === "1"' in c.get("/").text
    assert c.get("/api/connectors/catalog").json()["catalog"][0]["type"] == "pushover"
    assert c.get("/api/connectors").json() == {"connectors": []}
    r = c.put("/api/connectors/pushover", json={"fields": {"A": "b"}})
    assert r.json()["echo"] == {"fields": {"A": "b"}}
    assert c.delete("/api/connectors/pushover").status_code == 200
    # the browser never sees the provisioner token; the tool adds it
    assert all(call[2] == "Bearer prov-tok" for call in FakeProvisioner.calls)


def test_provisioner_errors_pass_through(monkeypatch, tmp_path, prov_server):
    m = load_app(monkeypatch, tmp_path, PROVISIONER_URL=prov_server, PROVISIONER_TOKEN="prov-tok")
    r = TestClient(m.app).put("/api/connectors/boom", json={"fields": {}})
    assert r.status_code == 422 and r.json() == {"error": "PUSHOVER_USER_KEY is required"}


def test_unreachable_provisioner_is_502(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path, PROVISIONER_URL="http://127.0.0.1:1", PROVISIONER_TOKEN="t")
    r = TestClient(m.app).get("/api/connectors")
    assert r.status_code == 502 and "unreachable" in r.json()["error"]


def test_config_tool_auth_guards_connectors(monkeypatch, tmp_path, prov_server):
    m = load_app(monkeypatch, tmp_path, PROVISIONER_URL=prov_server,
                 PROVISIONER_TOKEN="prov-tok", SIJBA_AUTH_TOKEN="ui-tok")
    c = TestClient(m.app)
    assert c.get("/api/connectors").status_code == 401
    assert c.get("/api/connectors", headers={"Authorization": "Bearer ui-tok"}).status_code == 200
