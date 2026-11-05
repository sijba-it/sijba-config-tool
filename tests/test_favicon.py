"""Favicon (app icon with a 'C' badge) is served without auth and linked from the UI."""
from fastapi.testclient import TestClient

from test_connectors import load_app


def test_favicon_served_and_linked(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path, ROOT_PATH="/config-ui", SIJBA_AUTH_TOKEN="secret")
    c = TestClient(m.app)
    for path in ("/config-ui/favicon.png", "/config-ui/apple-touch-icon.png"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")
    html = c.get("/config-ui/").text
    assert 'href="/config-ui/favicon.png"' in html
    assert 'href="/config-ui/apple-touch-icon.png"' in html


def test_favicon_ico_at_root_and_under_prefix(monkeypatch, tmp_path):
    m = load_app(monkeypatch, tmp_path, ROOT_PATH="/config-ui", SIJBA_AUTH_TOKEN="secret")
    c = TestClient(m.app)
    for path in ("/favicon.ico", "/config-ui/favicon.ico"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
        assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header
