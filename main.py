"""sijba-config-tool — edit sijba base config on a shared filesystem.

Phase 1: filesystem backend only (SIJBA_CONFIG_DIR). Later backends (e.g.
agent-controller via sijba metadata RPC) should expose the same list/get/put/
validate shape without assuming local paths in the UI.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

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


@api.get("/", response_class=HTMLResponse)
@api.get("", response_class=HTMLResponse)
def ui():
    return HTMLResponse(_UI_HTML.replace("__ROOT__", ROOT_PATH or ""))


# Mount under ROOT_PATH for ingress prefix (/config-ui); otherwise serve at /.
if ROOT_PATH:
    app.mount(ROOT_PATH, api)
else:
    app = api  # type: ignore[misc]


_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>sijba config</title>
<style>
  :root {
    --bg: #1a1d23;
    --panel: #242830;
    --border: #3a4050;
    --text: #e8eaed;
    --muted: #9aa0a6;
    --accent: #6db3f2;
    --ok: #7dcea0;
    --bad: #e07a7a;
    --mono: "JetBrains Mono", "Fira Code", ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "IBM Plex Sans", system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: baseline; gap: 1rem;
    padding: .75rem 1.25rem; border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  header h1 { font-size: 1.1rem; margin: 0; letter-spacing: .02em; }
  header .hint { color: var(--muted); font-size: .85rem; }
  #auth-bar {
    display: flex; gap: .5rem; align-items: center; margin-left: auto;
  }
  #auth-bar input {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: .35rem .55rem; border-radius: 4px; width: 14rem;
    font-family: var(--mono); font-size: .8rem;
  }
  main { display: grid; grid-template-columns: 16rem 1fr; flex: 1; min-height: 0; }
  #files {
    border-right: 1px solid var(--border); overflow: auto; padding: .5rem 0;
    background: var(--panel);
  }
  #files button {
    display: block; width: 100%; text-align: left; border: 0; background: transparent;
    color: var(--text); padding: .4rem 1rem; font: inherit; cursor: pointer;
    font-family: var(--mono); font-size: .8rem;
  }
  #files button:hover { background: #2c313c; }
  #files button.active { background: #343b4a; color: var(--accent); }
  #files .kind { color: var(--muted); font-size: .7rem; margin-right: .35rem; }
  #editor-wrap { display: flex; flex-direction: column; min-width: 0; }
  #toolbar {
    display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
    padding: .5rem 1rem; border-bottom: 1px solid var(--border);
  }
  #toolbar .path { font-family: var(--mono); font-size: .85rem; color: var(--accent); }
  #toolbar .spacer { flex: 1; }
  button.act {
    background: #3a4558; color: var(--text); border: 1px solid var(--border);
    padding: .35rem .75rem; border-radius: 4px; cursor: pointer; font: inherit;
  }
  button.act.primary { background: #2a5a8a; border-color: #3a7ab0; }
  button.act:disabled { opacity: .5; cursor: default; }
  #status { font-size: .85rem; color: var(--muted); }
  #status.ok { color: var(--ok); }
  #status.bad { color: var(--bad); }
  textarea {
    flex: 1; width: 100%; border: 0; resize: none; padding: 1rem;
    background: var(--bg); color: var(--text);
    font-family: var(--mono); font-size: .85rem; line-height: 1.45;
    tab-size: 2;
  }
  textarea:focus { outline: none; }
  @media (max-width: 720px) {
    main { grid-template-columns: 1fr; grid-template-rows: 10rem 1fr; }
    #files { border-right: 0; border-bottom: 1px solid var(--border); }
  }
</style>
</head>
<body>
<header>
  <h1>sijba config</h1>
  <span class="hint">filesystem · restart core to apply</span>
  <div id="auth-bar">
    <input id="token" type="password" placeholder="Bearer token" autocomplete="off"/>
  </div>
</header>
<main>
  <nav id="files"></nav>
  <section id="editor-wrap">
    <div id="toolbar">
      <span class="path" id="cur-path">—</span>
      <span class="spacer"></span>
      <span id="status"></span>
      <button class="act" id="btn-validate" disabled>Validate</button>
      <button class="act primary" id="btn-save" disabled>Save</button>
    </div>
    <textarea id="editor" spellcheck="false" disabled placeholder="Select a file…"></textarea>
  </section>
</main>
<script>
const ROOT = "__ROOT__";
const base = ROOT || "";
let current = null;
let token = localStorage.getItem("sijba_config_token") || "";
const $token = document.getElementById("token");
$token.value = token;
$token.addEventListener("change", () => {
  token = $token.value.trim();
  localStorage.setItem("sijba_config_token", token);
  loadList();
});

function headers() {
  const h = {"Content-Type": "application/json"};
  if (token) h["Authorization"] = "Bearer " + token;
  return h;
}

function setStatus(msg, cls) {
  const el = document.getElementById("status");
  el.textContent = msg || "";
  el.className = cls || "";
}

async function api(path, opts) {
  const r = await fetch(base + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.statusText);
  return data;
}

async function loadList() {
  const nav = document.getElementById("files");
  nav.innerHTML = "";
  try {
    const data = await api("/api/files", {headers: headers()});
    for (const f of data.files) {
      const b = document.createElement("button");
      b.innerHTML = '<span class="kind">' + f.kind + '</span>' + f.path;
      if (f.path === current) b.classList.add("active");
      b.onclick = () => openFile(f.path);
      nav.appendChild(b);
    }
  } catch (e) {
    setStatus(String(e.message || e), "bad");
  }
}

async function openFile(path) {
  current = path;
  document.getElementById("cur-path").textContent = path;
  document.querySelectorAll("#files button").forEach(b => {
    b.classList.toggle("active", b.textContent.endsWith(path) || b.textContent.includes(path));
  });
  try {
    const data = await api("/api/files/" + encodeURI(path), {headers: headers()});
    const ed = document.getElementById("editor");
    ed.value = data.content;
    ed.disabled = false;
    document.getElementById("btn-save").disabled = false;
    document.getElementById("btn-validate").disabled = false;
    setStatus("loaded", "ok");
    loadList();
  } catch (e) {
    setStatus(String(e.message || e), "bad");
  }
}

document.getElementById("btn-validate").onclick = async () => {
  if (!current) return;
  try {
    const report = await api("/api/validate/" + encodeURI(current), {
      method: "POST", headers: headers(),
      body: JSON.stringify({content: document.getElementById("editor").value}),
    });
    if (report.ok) setStatus("valid (" + report.kind + ")", "ok");
    else setStatus(report.errors.join("; "), "bad");
  } catch (e) {
    setStatus(String(e.message || e), "bad");
  }
};

document.getElementById("btn-save").onclick = async () => {
  if (!current) return;
  try {
    const res = await api("/api/files/" + encodeURI(current), {
      method: "PUT", headers: headers(),
      body: JSON.stringify({content: document.getElementById("editor").value, backup: true}),
    });
    setStatus("saved · " + res.bytes + " bytes · restart core to apply", "ok");
  } catch (e) {
    setStatus(String(e.message || e), "bad");
  }
};

document.getElementById("editor").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") {
    e.preventDefault();
    document.getElementById("btn-save").click();
  }
});

loadList();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
