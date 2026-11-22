"""Which agents may send messages to other channels (Core's can_inject permission)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import agents
from test_apply_webhooks import AUTH
from test_connectors import load_app


class FakeCore(BaseHTTPRequestHandler):
    permissions: dict = {}
    patches: list = []
    reject = False

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/plugin/channels":
            self._send(200, [{"channel_id": "cursor:obsidian", "label": "Obsidian", "triggers": ["@obsidian"]},
                             {"channel_id": "cursor:recherche", "triggers": []}])
        elif self.path == "/api/admin/channel-permissions":
            self._send(200, FakeCore.permissions)
        else:
            self._send(404, {})

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakeCore.patches.append((self.path, body, self.headers.get("Authorization")))
        if FakeCore.reject:
            self._send(403, {"detail": "no"})
            return
        cid = self.path[len("/api/admin/channel-permissions/"):]
        FakeCore.permissions.setdefault(cid, {}).update(body)
        self._send(200, {"channel_id": cid, **FakeCore.permissions[cid]})

    def log_message(self, *a):
        pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeCore.permissions = {"cursor:obsidian": {"can_inject": True}, "old:gone": {"can_inject": True}}
    FakeCore.patches = []
    FakeCore.reject = False
    srv = HTTPServer(("127.0.0.1", 0), FakeCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL=f"http://127.0.0.1:{srv.server_port}")
    yield TestClient(m.app)
    srv.shutdown()


def test_overview_merges_channels_and_permissions():
    out = agents.overview(
        [{"channel_id": "a:1", "triggers": ["@one"]}, {"channel_id": "a:1"}, {"channel_id": "b:2", "label": "Two"}],
        {"b:2": {"can_inject": True}, "gone:3": {"can_inject": True}, "off:4": {"can_inject": False}})
    assert [(a["channel_id"], a["can_inject"], a["online"]) for a in out] == [
        ("a:1", False, True), ("b:2", True, True), ("gone:3", True, False), ("off:4", False, False)]
    assert out[0]["mention"] == "@one" and out[1]["label"] == "Two"


def test_agents_endpoint(client):
    r = client.get("/api/agents", headers=AUTH).json()
    assert r["core_error"] is None
    assert "sijba-inject" in r["format"]
    by_id = {a["channel_id"]: a for a in r["agents"]}
    assert by_id["cursor:obsidian"]["can_inject"] is True and by_id["cursor:obsidian"]["mention"] == "@obsidian"
    assert by_id["cursor:recherche"]["can_inject"] is False
    assert by_id["old:gone"]["online"] is False


def test_agents_needs_login(client):
    assert client.get("/api/agents").status_code == 401
    assert client.put("/api/agents/permissions/cursor:obsidian", json={"can_inject": False}).status_code == 401


def test_set_permission_goes_to_core(client):
    r = client.put("/api/agents/permissions/cursor:recherche", headers=AUTH, json={"can_inject": True})
    assert r.status_code == 200 and r.json() == {"channel_id": "cursor:recherche", "can_inject": True}
    assert FakeCore.patches == [("/api/admin/channel-permissions/cursor:recherche", {"can_inject": True},
                                 "Bearer ui-tok")]
    again = {a["channel_id"]: a["can_inject"] for a in client.get("/api/agents", headers=AUTH).json()["agents"]}
    assert again["cursor:recherche"] is True


def test_core_refusal_is_502(client):
    FakeCore.reject = True
    r = client.put("/api/agents/permissions/cursor:obsidian", headers=AUTH, json={"can_inject": False})
    assert r.status_code == 502 and "Core refused" in r.json()["error"]


def test_core_down_is_reported(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL="http://127.0.0.1:1")
    c = TestClient(m.app)
    r = c.get("/api/agents", headers=AUTH).json()
    assert r["agents"] == [] and "not reachable" in r["core_error"]
    assert c.put("/api/agents/permissions/x:y", headers=AUTH, json={"can_inject": True}).status_code == 502
