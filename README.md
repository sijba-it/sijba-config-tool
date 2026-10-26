# sijba-config-tool

Web UI + small API to edit **sijba base config** on a shared filesystem
(`SIJBA_CONFIG_DIR`). Phase 1 only — filesystem backend. A later backend
(e.g. agent-controller over sijba `metadata` RPC) can reuse the same
list / get / put / validate shape.

## What it edits

| Path | Notes |
|------|--------|
| `core.yaml` | YAML parse check |
| `handlers/*.json` | JSON + `handler.schema.json` |
| `routes/*.json` | JSON array |

Writes are atomic (`.tmp` → rename) and leave a `.bak` beside the file.
**Core does not hot-reload** — restart the sijba process/pod after save.

The UI has **Form** and **Raw** tabs. Form is file-kind specific (handler
fields / routes list / core.yaml options); unknown keys are preserved when
saving from the form. Broken files stay editable in Raw.

## Local run

```bash
cd sijba-config-tool
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SIJBA_CONFIG_DIR=/path/to/sijba-config
export SIJBA_AUTH_TOKEN=secret   # optional; if set, Bearer required
export PORT=8081
# export ROOT_PATH=/config-ui    # only behind an ingress path prefix
# export SIJBA_CORE_URL=http://127.0.0.1:8080   # for "Apply changes" (Core /api/admin/reload)
# export PUBLIC_CORE_URL=https://sijba.example  # shown in webhook URLs; default: the page's origin

python main.py
```

Open `http://localhost:8081/`.

## API

| Method | Path | |
|--------|------|--|
| `GET` | `/api/health` | no auth |
| `GET` | `/api/files` | list |
| `GET` | `/api/files/{path}` | read |
| `PUT` | `/api/files/{path}` | write (`{"content":"…"}`) |
| `POST` | `/api/validate/{path}` | validate (`{"content":"…"}` optional) |
| `POST` | `/api/parse/{path}` | parse → `{kind, data}` for the form |
| `POST` | `/api/serialize/{path}` | `{data}` → `{content}` |
| `GET` | `/api/me` | token check for the login screen |
| `POST` | `/api/apply` | asks Core to reload its config (`POST /api/admin/reload`) |
| `GET`/`POST` | `/api/webhooks` | list / add a named webhook (`{"name"}`, token generated) in `core.yaml` |
| `DELETE` | `/api/webhooks/{name}` | remove it |

The UI has two pages: **Connections** (connectors, webhooks) and **Advanced**
(the file editor). Saving a file or webhook marks "Apply changes"; applying
reloads Core without a restart and reports files it skipped and settings that
still need a restart (`port`, `db_path`, …).

If `ROOT_PATH` is set (k8s: `/config-ui`), all of the above are under that prefix.

## Connectors (optional)

If `PROVISIONER_URL` **and** `PROVISIONER_TOKEN` are set, the UI shows a
"Apps & services" section (Pushover, Telegram, …): list the catalog, activate with the form,
deactivate. The tool only speaks the provisioner API
([sijba-provisioner-k8s](../sijba-provisioner-k8s) or a hosted one) — it never
touches Kubernetes, and the provisioner token never reaches the browser.
Secret fields are write-only: the UI shows "set", never the value. Without
those env vars the tab and the endpoints (`/api/connectors*`) don't exist.

## Docker / k8s

Jenkins on `main` (same pattern as Core): test → multiarch build/push
(`REDACTED-CRED-ID`) → bump sidecar image in `edvbartl-ansible` StatefulSet →
ArgoCD refresh `sijba` (`REDACTED-CRED-ID`).

```bash
docker build -t REDACTED-REGISTRY/sijba-config-tool:manual .
docker push REDACTED-REGISTRY/sijba-config-tool:manual
```

Runs as a **sidecar** in the sijba StatefulSet, sharing `pvc-sijba-data`.
Config lives at `/data/config` (seeded once from the ConfigMap). UI:
`https://sijba.edv-bartl.at/config-ui/`.

## Intentionally not here

- Hot-reload / restart of Core
- Agent-controller config (later, via sijba — not filesystem)
- Editing secrets that belong in env (`SIJBA_AUTH_TOKEN`, API keys)

