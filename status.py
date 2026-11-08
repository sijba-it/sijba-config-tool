"""Problems per config file, shown next to the file until they are fixed.

Three sources:
  check    the tool's own validation (JSON, schema, filename, unreadable routes,
           regexes that don't compile) — always current
  sijba    files Core skipped at its last load (`/api/admin/load-errors`) —
           stays until the next Apply after the fix; hidden while `check`
           already reports the file
  running  handlers Core reports as degraded / stopped
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from fs_backend import FsBackend

BAD_STATES = {"degraded", "stopped", "restarting"}


def _add(out: dict, path: str, source: str, message: str) -> None:
    out.setdefault(path, []).append({"source": source, "message": message})


def collect(backend: FsBackend, steps: list[dict], core_errors: list[str] | None,
            handler_states: dict[str, str] | None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    files = backend.list_files()
    for f in files:
        try:
            report = backend.validate(f["path"])
        except Exception as exc:  # unreadable file: report, don't fail the page
            _add(out, f["path"], "check", str(exc))
            continue
        for err in report["errors"]:
            _add(out, f["path"], "check", err)

    for s in steps:
        if s["kind"] == "rules" and not s["editable"] and s["note"]:
            _add(out, s["file"], "check", s["note"])
        for i, rule in enumerate(s.get("rules") or [], 1):
            if rule.get("when") == "regex":
                try:
                    re.compile(rule["pattern"])
                except re.error as exc:
                    _add(out, s.get("routes_file") or s["file"], "check", f"rule {i}: invalid regex ({exc})")

    by_name = {PurePosixPath(f["path"]).name: f["path"] for f in files if f["kind"] == "handler"}
    for err in core_errors or []:
        name, _, msg = str(err).partition(": ")
        path = by_name.get(name)
        # Same problem the own check already shows → once is enough. Without an
        # own finding (fixed, not applied yet) Core's message is the useful one.
        if path and not any(p["source"] == "check" for p in out.get(path, [])):
            _add(out, path, "sijba", msg or err)

    by_id = {s["id"]: s["file"] for s in steps}
    for hid, st in (handler_states or {}).items():
        if st in BAD_STATES and hid in by_id:
            _add(out, by_id[hid], "running", f"sijba reports this handler as {st}")
    return out
