"""Filesystem backend for sijba config files.

Reads/writes under SIJBA_CONFIG_DIR. Path allowlist keeps the tool from
touching DB/uploads on the same PVC. Designed as a swap-in target later
(e.g. remote backend) — callers use list/get/put/validate only.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore

_HANDLER_NAME = re.compile(r"^\d+(\.\d+)?-[A-Za-z0-9._-]+\.json$")
_ROUTE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.json$")


class ConfigError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class FsBackend:
    def __init__(self, root: Path, schema_path: Optional[Path] = None):
        self.root = root.resolve()
        self.schema_path = schema_path
        self._schema: Optional[dict] = None

    def _load_schema(self) -> Optional[dict]:
        if self._schema is not None:
            return self._schema
        if not self.schema_path or not self.schema_path.is_file():
            return None
        with open(self.schema_path) as fh:
            self._schema = json.load(fh)
        return self._schema

    def resolve(self, rel: str) -> Path:
        """Map a relative config path to an absolute path; reject escapes."""
        rel = (rel or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/") or rel.startswith("."):
            raise ConfigError("invalid path", 400)
        if not self._allowed(rel):
            raise ConfigError(f"path not allowed: {rel}", 403)
        path = (self.root / rel).resolve()
        if not str(path).startswith(str(self.root) + os.sep) and path != self.root:
            raise ConfigError("path escapes config root", 403)
        return path

    def _allowed(self, rel: str) -> bool:
        """Allowlisted relative paths. Handler filenames should be NN.N-id.json
        but any `handlers/*.json` is readable so a misnamed file can be repaired.
        """
        if rel == "core.yaml":
            return True
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] == "handlers" and parts[1].endswith(".json"):
            return bool(re.match(r"^[A-Za-z0-9._-]+$", parts[1]))
        if len(parts) == 2 and parts[0] == "routes" and _ROUTE_NAME.match(parts[1]):
            return True
        return False

    def list_files(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        core = self.root / "core.yaml"
        if core.is_file():
            out.append({"path": "core.yaml", "kind": "core", "bytes": core.stat().st_size})
        handlers = self.root / "handlers"
        if handlers.is_dir():
            for p in sorted(handlers.glob("*.json")):
                if _HANDLER_NAME.match(p.name):
                    out.append({"path": f"handlers/{p.name}", "kind": "handler", "bytes": p.stat().st_size})
                else:
                    out.append({
                        "path": f"handlers/{p.name}",
                        "kind": "handler",
                        "bytes": p.stat().st_size,
                        "warning": "filename does not match NN.N-id.json",
                    })
        routes = self.root / "routes"
        if routes.is_dir():
            for p in sorted(routes.glob("*.json")):
                out.append({"path": f"routes/{p.name}", "kind": "route", "bytes": p.stat().st_size})
        return out

    def get(self, rel: str) -> dict[str, Any]:
        path = self.resolve(rel)
        if not path.is_file():
            # Allow reading missing files only when the path is allowlisted
            # (UI can create new handlers). Still 404 so the client knows.
            raise ConfigError(f"not found: {rel}", 404)
        text = path.read_text(encoding="utf-8")
        return {"path": rel, "content": text, "bytes": len(text.encode())}

    def put(self, rel: str, content: str, *, backup: bool = True) -> dict[str, Any]:
        path = self.resolve(rel)
        # Validate before write so we never persist garbage as the only copy.
        report = self.validate(rel, content)
        if not report["ok"]:
            raise ConfigError("; ".join(report["errors"]), 400)

        path.parent.mkdir(parents=True, exist_ok=True)
        if backup and path.is_file():
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return {"path": rel, "bytes": path.stat().st_size, "validation": report}

    def validate(self, rel: str, content: Optional[str] = None) -> dict[str, Any]:
        if content is None:
            path = self.resolve(rel)
            if not path.is_file():
                raise ConfigError(f"not found: {rel}", 404)
            content = path.read_text(encoding="utf-8")
        else:
            # Ensure path shape is legal even when content is supplied.
            self.resolve(rel)

        errors: list[str] = []
        kind = "core" if rel == "core.yaml" else (
            "handler" if rel.startswith("handlers/") else "route"
        )

        if kind == "core":
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                return {"ok": False, "kind": kind, "errors": [f"YAML: {exc}"]}
            if data is None:
                data = {}
            if not isinstance(data, dict):
                errors.append("core.yaml must be a mapping")
        elif kind in ("handler", "route"):
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                return {"ok": False, "kind": kind, "errors": [f"JSON: {exc}"]}
            if kind == "handler":
                if not isinstance(data, dict):
                    errors.append("handler must be a JSON object")
                else:
                    schema = self._load_schema()
                    if schema and jsonschema is not None:
                        try:
                            jsonschema.validate(data, schema)
                        except jsonschema.ValidationError as exc:
                            errors.append(f"schema: {exc.message}")
                    name = Path(rel).name
                    if not _HANDLER_NAME.match(name):
                        errors.append(
                            f"filename {name!r} should look like 10.0-my-handler.json"
                        )
            else:
                if not isinstance(data, list):
                    errors.append("routes file must be a JSON array")
        else:
            errors.append(f"unknown kind for {rel}")

        return {"ok": not errors, "kind": kind, "errors": errors}
