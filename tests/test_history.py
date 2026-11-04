"""Change history: every write is recorded, grouped per action, and can be undone."""
import json

import pytest
from fastapi.testclient import TestClient

from history import History
from test_apply_webhooks import AUTH
from test_connectors import load_app

ECHO = {"id": "echo", "priority": 99, "direction": "inbound", "type": "exec_json", "exec": "x"}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    (tmp_path / "core.yaml").write_text("port: 8080\n")
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL="http://127.0.0.1:1")
    return TestClient(m.app), tmp_path


def _put(c, path, data):
    r = c.put("/api/files/" + path, headers=AUTH, json={"content": json.dumps(data)})
    assert r.status_code == 200, r.text


def test_saves_are_listed_newest_first_with_diffs(cfg):
    c, _ = cfg
    _put(c, "handlers/99.0-echo.json", ECHO)
    _put(c, "handlers/99.0-echo.json", {**ECHO, "timeout_seconds": 5})
    _put(c, "handlers/99.0-echo.json", {**ECHO, "timeout_seconds": 5})  # unchanged → no entry
    changes = c.get("/api/history", headers=AUTH).json()["changes"]
    assert [(x["label"], x["files"][0]["action"]) for x in changes] == [
        ("Saved handlers/99.0-echo.json", "changed"),
        ("Created handlers/99.0-echo.json", "added"),
    ]
    detail = c.get("/api/history/" + changes[0]["id"], headers=AUTH).json()
    f = detail["files"][0]
    assert json.loads(f["before"]) == ECHO and json.loads(f["after"])["timeout_seconds"] == 5
    assert f["changed_since"] is False
    assert c.get("/api/history/nope", headers=AUTH).status_code == 404


def test_undo_a_template_removes_all_its_files_in_one_go(cfg):
    c, d = cfg
    r = c.post("/api/routing/presets/voice-notes", headers=AUTH,
               json={"variant": "copy", "params": {"source": "webhook:voice", "target": "@obsidian"}})
    assert r.status_code == 200
    change = c.get("/api/history", headers=AUTH).json()["changes"][0]
    assert change["label"] == "Routing: template voice-notes added"
    assert sorted(f["action"] for f in change["files"]) == ["added", "added"]

    assert c.post(f"/api/history/{change['id']}/undo", headers=AUTH).status_code == 200
    assert list((d / "handlers").glob("*.json")) == []
    undo = c.get("/api/history", headers=AUTH).json()["changes"][0]
    assert undo["label"] == "Undo: Routing: template voice-notes added"
    assert sorted(f["action"] for f in undo["files"]) == ["deleted", "deleted"]
    # and undoing the undo brings them back
    c.post(f"/api/history/{undo['id']}/undo", headers=AUTH)
    assert len(list((d / "handlers").glob("*.json"))) == 2


def test_undo_restores_a_deleted_file_and_flags_later_edits(cfg):
    c, d = cfg
    _put(c, "handlers/99.0-echo.json", ECHO)
    created = c.get("/api/history", headers=AUTH).json()["changes"][0]["id"]
    _put(c, "handlers/99.0-echo.json", {**ECHO, "timeout_seconds": 7})
    assert c.get("/api/history/" + created, headers=AUTH).json()["files"][0]["changed_since"] is True

    assert c.delete("/api/files/handlers/99.0-echo.json", headers=AUTH).status_code == 200
    deleted = c.get("/api/history", headers=AUTH).json()["changes"][0]
    assert deleted["label"] == "Deleted handlers/99.0-echo.json"
    c.post(f"/api/history/{deleted['id']}/undo", headers=AUTH)
    assert json.loads((d / "handlers/99.0-echo.json").read_text())["timeout_seconds"] == 7


def test_move_is_one_change(cfg):
    c, d = cfg
    _put(c, "handlers/10.0-a.json", {**ECHO, "id": "a", "priority": 10})
    _put(c, "handlers/20.0-b.json", {**ECHO, "id": "b", "priority": 20})
    c.post("/api/routing/move", headers=AUTH, json={"source": "web", "file": "handlers/20.0-b.json", "index": 0})
    change = c.get("/api/history", headers=AUTH).json()["changes"][0]
    assert change["label"] == "Routing: b moved"
    assert sorted((f["path"], f["action"]) for f in change["files"]) == [
        ("handlers/0.0-b.json", "added"), ("handlers/20.0-b.json", "deleted")]


def test_webhook_change_is_labelled(cfg):
    c, _ = cfg
    c.post("/api/webhooks", headers=AUTH, json={"name": "pebble"})
    assert c.get("/api/history", headers=AUTH).json()["changes"][0]["label"] == "Webhook pebble added"


def test_history_is_not_reachable_through_the_file_api(cfg):
    c, _ = cfg
    _put(c, "handlers/99.0-echo.json", ECHO)
    assert c.get("/api/files/.history/log.jsonl", headers=AUTH).status_code in (400, 403)
    assert all(not f["path"].startswith(".") for f in c.get("/api/files", headers=AUTH).json()["files"])


def test_prune_keeps_the_newest_and_drops_unused_blobs(tmp_path):
    h = History(tmp_path / ".history", max_changes=3)
    for i in range(6):
        h.record("core.yaml", f"v{i}\n", f"v{i + 1}\n")
    assert [x["label"] for x in h.list()] == ["Saved core.yaml"] * 3
    assert len(list((tmp_path / ".history/blobs").iterdir())) == 4  # v3..v6
