"""Plaintext auth_token in core.yaml is hashed on write; the tool's own login may be a hash."""
import yaml
from fastapi.testclient import TestClient

import authhash
from test_apply_webhooks import AUTH
from test_connectors import load_app


def _app(monkeypatch, tmp_path, core, **env):
    (tmp_path / "core.yaml").write_text(core)
    m = load_app(monkeypatch, tmp_path, SIJBA_CORE_URL="http://127.0.0.1:1", **env)
    return TestClient(m.app)


def test_save_hashes_plaintext_and_keeps_comments(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, "port: 8080\n", SIJBA_AUTH_TOKEN="ui-tok")
    r = c.put("/api/files/core.yaml", headers=AUTH, json={"content": "# mine\nport: 8080\nauth_token: hunter2-hunter2\n"})
    assert r.status_code == 200
    text = (tmp_path / "core.yaml").read_text()
    assert text.startswith("# mine\n") and "hunter2" not in text
    h = yaml.safe_load(text)["auth_token"]
    assert h.startswith("scrypt$") and authhash.verify("hunter2-hunter2", h)


def test_long_token_gets_sha256_and_hash_is_stable(monkeypatch, tmp_path):
    secret = "x" * 40
    c = _app(monkeypatch, tmp_path, "port: 8080\n", SIJBA_AUTH_TOKEN="ui-tok")
    c.put("/api/files/core.yaml", headers=AUTH, json={"content": f"auth_token: {secret}\n"})
    h = yaml.safe_load((tmp_path / "core.yaml").read_text())["auth_token"]
    assert h == authhash.hash_sha256(secret)
    c.put("/api/files/core.yaml", headers=AUTH, json={"content": f"auth_token: {h}\nport: 1\n"})
    assert yaml.safe_load((tmp_path / "core.yaml").read_text())["auth_token"] == h  # no double hashing


def test_env_reference_untouched(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, "port: 8080\n", SIJBA_AUTH_TOKEN="ui-tok")
    c.put("/api/files/core.yaml", headers=AUTH, json={"content": "auth_token: ${SIJBA_AUTH_TOKEN}\n"})
    assert "${SIJBA_AUTH_TOKEN}" in (tmp_path / "core.yaml").read_text()


def test_harden_endpoint_migrates_existing_and_scrubs_bak(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, "auth_token: plain-old-secret\n", SIJBA_AUTH_TOKEN="ui-tok")
    assert c.get("/api/auth", headers=AUTH).json() == {"set": True, "hashed": False, "env_ref": False}
    assert c.post("/api/auth/harden", headers=AUTH).json() == {"changed": True}
    assert c.get("/api/auth", headers=AUTH).json()["hashed"] is True
    assert c.post("/api/auth/harden", headers=AUTH).json() == {"changed": False}
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert "plain-old-secret" not in f.read_text(errors="ignore"), f


def test_tool_login_can_be_a_hash(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, "port: 1\n", SIJBA_AUTH_TOKEN=authhash.hash_sha256("ui-tok"))
    assert c.get("/api/files", headers=AUTH).status_code == 200
    assert c.get("/api/files", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_restricted_mode_needs_admin_token_for_files(monkeypatch, tmp_path):
    (tmp_path / "core.yaml").write_text("port: 1\n")
    m = load_app(monkeypatch, tmp_path, SIJBA_CORE_URL="http://127.0.0.1:1", SIJBA_AUTH_TOKEN="user-tok",
                 SIJBA_RESTRICTED="1", SIJBA_ADMIN_TOKEN="adm-tok")
    c = TestClient(m.app)
    user, admin = {"Authorization": "Bearer user-tok"}, {"Authorization": "Bearer adm-tok"}
    assert c.get("/api/me", headers=user).json() == {"ok": True, "connectors": False, "admin": False, "restricted": True}
    assert c.get("/api/me", headers=admin).json()["admin"] is True
    for method, path in [("get", "/api/files"), ("get", "/api/files/core.yaml"),
                         ("put", "/api/files/core.yaml"), ("post", "/api/parse/core.yaml"),
                         ("post", "/api/auth/harden")]:
        r = getattr(c, method)(path, headers=user, **({"json": {"content": "x", "data": {}}} if method != "get" else {}))
        assert r.status_code == 403, (method, path)
    assert c.get("/api/files", headers=admin).status_code == 200
    assert c.get("/api/routing", headers=user).status_code in (200, 502)  # allowed for users
    assert c.get("/api/files", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_unrestricted_mode_everyone_is_admin(monkeypatch, tmp_path):
    (tmp_path / "core.yaml").write_text("port: 1\n")
    m = load_app(monkeypatch, tmp_path, SIJBA_CORE_URL="http://127.0.0.1:1", SIJBA_AUTH_TOKEN="user-tok")
    c = TestClient(m.app)
    assert c.get("/api/files", headers={"Authorization": "Bearer user-tok"}).status_code == 200
    assert c.get("/api/me", headers={"Authorization": "Bearer user-tok"}).json()["admin"] is True
