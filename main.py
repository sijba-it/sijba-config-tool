"""sijba-config-tool — edit sijba base config on a shared filesystem.

Phase 1: filesystem backend only (SIJBA_CONFIG_DIR). Later backends (e.g.
agent-controller via sijba metadata RPC) should expose the same list/get/put/
validate shape without assuming local paths in the UI.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from codec import parse_content, serialize_content
from fs_backend import ConfigError, FsBackend
from provisioner import Provisioner, ProvisionerError

CONFIG_DIR = Path(os.environ.get("SIJBA_CONFIG_DIR", "./sijba-config"))
AUTH_TOKEN = os.environ.get("SIJBA_AUTH_TOKEN") or None
ROOT_PATH = (os.environ.get("ROOT_PATH") or "").rstrip("/")
SCHEMA_PATH = Path(
    os.environ.get(
        "HANDLER_SCHEMA_PATH",
        str(Path(__file__).parent / "handler.schema.json"),
    )
)
PORT = int(os.environ.get("PORT", "8081"))
CORE_URL = (os.environ.get("SIJBA_CORE_URL") or "http://127.0.0.1:8080").rstrip("/")
PUBLIC_CORE_URL = (os.environ.get("PUBLIC_CORE_URL") or "").rstrip("/")
WEBHOOK_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
UI_PATH = Path(__file__).parent / "static" / "index.html"

backend = FsBackend(CONFIG_DIR, schema_path=SCHEMA_PATH)

PROVISIONER_URL = (os.environ.get("PROVISIONER_URL") or "").strip()
PROVISIONER_TOKEN = (os.environ.get("PROVISIONER_TOKEN") or "").strip()
provisioner = (
    Provisioner(PROVISIONER_URL, PROVISIONER_TOKEN)
    if PROVISIONER_URL and PROVISIONER_TOKEN else None
)

app = FastAPI(title="sijba-config-tool", docs_url=None, redoc_url=None)
api = FastAPI(title="sijba-config-tool")


def _check_auth(authorization: str | None = Header(default=None),
                token: str | None = None) -> None:
    if not AUTH_TOKEN:
        return
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    got = bearer or token
    if got != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


class PutBody(BaseModel):
    content: str = Field(..., min_length=0)
    backup: bool = True


class ValidateBody(BaseModel):
    content: str | None = None


class ParseBody(BaseModel):
    content: str


class SerializeBody(BaseModel):
    data: Any


@api.exception_handler(ConfigError)
async def _config_error(_request: Request, exc: ConfigError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@api.get("/api/health")
def health():
    return {
        "ok": True,
        "config_dir": str(CONFIG_DIR),
        "exists": CONFIG_DIR.is_dir(),
    }


@api.get("/api/files", dependencies=[Depends(_check_auth)])
def list_files():
    return {"files": backend.list_files()}


@api.get("/api/files/{path:path}", dependencies=[Depends(_check_auth)])
def get_file(path: str):
    return backend.get(path)


@api.put("/api/files/{path:path}", dependencies=[Depends(_check_auth)])
def put_file(path: str, body: PutBody):
    return backend.put(path, body.content, backup=body.backup)


@api.post("/api/validate/{path:path}", dependencies=[Depends(_check_auth)])
def validate_file(path: str, body: ValidateBody | None = None):
    content = body.content if body else None
    return backend.validate(path, content)


@api.post("/api/parse/{path:path}", dependencies=[Depends(_check_auth)])
def parse_file(path: str, body: ParseBody):
    backend.resolve(path)  # allowlist
    return parse_content(path, body.content)


@api.post("/api/serialize/{path:path}", dependencies=[Depends(_check_auth)])
def serialize_file(path: str, body: SerializeBody):
    backend.resolve(path)
    return {"content": serialize_content(path, body.data)}


@api.get("/api/me", dependencies=[Depends(_check_auth)])
def me():
    return {"ok": True, "connectors": provisioner is not None}


@api.post("/api/apply", dependencies=[Depends(_check_auth)])
def apply_config():
    """Ask Core to re-read its config files (POST /api/admin/reload)."""
    req = urllib.request.Request(
        CORE_URL + "/api/admin/reload", data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return JSONResponse({"error": f"Core reload failed ({exc.code}): {detail}"}, status_code=502)
    except (urllib.error.URLError, OSError) as exc:
        return JSONResponse({"error": f"Core unreachable: {exc}"}, status_code=502)


def _core_yaml() -> dict:
    try:
        content = backend.get("core.yaml")["content"]
    except ConfigError as exc:
        if exc.status != 404:
            raise
        content = ""
    return parse_content("core.yaml", content)["data"]


def _save_core_yaml(data: dict) -> None:
    backend.put("core.yaml", serialize_content("core.yaml", data), backup=True)


@api.get("/api/webhooks", dependencies=[Depends(_check_auth)])
def webhooks_list():
    hooks = _core_yaml().get("webhooks") or {}
    return {"webhooks": [
        {"name": str(n), "token": str(t), "from_env": "${" in str(t)}
        for n, t in hooks.items()
    ]}


class WebhookBody(BaseModel):
    name: str


@api.post("/api/webhooks", dependencies=[Depends(_check_auth)])
def webhooks_add(body: WebhookBody):
    name = body.name.strip().lower()
    if not WEBHOOK_NAME.match(name):
        raise HTTPException(422, "name: lowercase letters, digits, - and _ (max 32)")
    data = _core_yaml()
    hooks = dict(data.get("webhooks") or {})
    if name in hooks:
        raise HTTPException(409, f"webhook {name!r} exists")
    hooks[name] = secrets.token_urlsafe(24)
    data["webhooks"] = hooks
    _save_core_yaml(data)
    return {"name": name, "token": hooks[name], "from_env": False}


@api.delete("/api/webhooks/{name}", dependencies=[Depends(_check_auth)])
def webhooks_delete(name: str):
    data = _core_yaml()
    hooks = dict(data.get("webhooks") or {})
    if name not in hooks:
        raise HTTPException(404, "not found")
    del hooks[name]
    if hooks:
        data["webhooks"] = hooks
    else:
        data.pop("webhooks", None)
    _save_core_yaml(data)
    return {"ok": True}


class ConnectorBody(BaseModel):
    fields: dict = Field(default_factory=dict)


def _need_provisioner() -> Provisioner:
    if provisioner is None:
        raise HTTPException(status_code=404, detail="connectors not configured")
    return provisioner


@api.exception_handler(ProvisionerError)
async def _provisioner_error(_request: Request, exc: ProvisionerError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@api.get("/api/connectors/catalog", dependencies=[Depends(_check_auth)])
def connectors_catalog():
    return _need_provisioner().catalog()


@api.get("/api/connectors", dependencies=[Depends(_check_auth)])
def connectors_list():
    return _need_provisioner().list()


@api.put("/api/connectors/{ctype}", dependencies=[Depends(_check_auth)])
def connectors_put(ctype: str, body: ConnectorBody):
    return _need_provisioner().put(ctype, body.fields)


@api.delete("/api/connectors/{ctype}", dependencies=[Depends(_check_auth)])
def connectors_delete(ctype: str):
    return _need_provisioner().delete(ctype)


@api.get("/", response_class=HTMLResponse)
@api.get("", response_class=HTMLResponse)
def ui():
    html = UI_PATH.read_text(encoding="utf-8")
    # Inject the ingress prefix once. Do not reuse a placeholder that also
    # appears in a JS sentinel check — replace() would break `base`.
    html = html.replace("__CONNECTORS__", "1" if provisioner else "0")
    html = html.replace("__PUBLIC_CORE_URL__", PUBLIC_CORE_URL)
    return HTMLResponse(html.replace("__BASE_PATH__", ROOT_PATH or ""))


if ROOT_PATH:
    app.mount(ROOT_PATH, api)
else:
    app = api  # type: ignore[misc]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
