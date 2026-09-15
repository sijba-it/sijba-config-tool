"""sijba-config-tool — edit sijba base config on a shared filesystem.

Phase 1: filesystem backend only (SIJBA_CONFIG_DIR). Later backends (e.g.
agent-controller via sijba metadata RPC) should expose the same list/get/put/
validate shape without assuming local paths in the UI.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from codec import parse_content, serialize_content
from fs_backend import ConfigError, FsBackend

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
UI_PATH = Path(__file__).parent / "static" / "index.html"

backend = FsBackend(CONFIG_DIR, schema_path=SCHEMA_PATH)

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


@api.get("/", response_class=HTMLResponse)
@api.get("", response_class=HTMLResponse)
def ui():
    html = UI_PATH.read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ROOT__", ROOT_PATH or ""))


if ROOT_PATH:
    app.mount(ROOT_PATH, api)
else:
    app = api  # type: ignore[misc]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
