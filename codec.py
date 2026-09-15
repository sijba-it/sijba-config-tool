"""Parse/serialize helpers for form ↔ raw editing."""
from __future__ import annotations

import json
from typing import Any

import yaml

from fs_backend import ConfigError


def file_kind(rel: str) -> str:
    if rel == "core.yaml":
        return "core"
    if rel.startswith("handlers/"):
        return "handler"
    if rel.startswith("routes/"):
        return "route"
    raise ConfigError(f"unknown kind for {rel}", 400)


def parse_content(rel: str, content: str) -> dict[str, Any]:
    kind = file_kind(rel)
    if kind == "core":
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML: {exc}", 400) from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError("core.yaml must be a mapping", 400)
        return {"kind": kind, "data": data}

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON: {exc}", 400) from exc

    if kind == "handler":
        if not isinstance(data, dict):
            raise ConfigError("handler must be a JSON object", 400)
    elif kind == "route":
        if not isinstance(data, list):
            raise ConfigError("routes file must be a JSON array", 400)
    return {"kind": kind, "data": data}


def serialize_content(rel: str, data: Any) -> str:
    kind = file_kind(rel)
    if kind == "core":
        if not isinstance(data, dict):
            raise ConfigError("core data must be an object", 400)
        # Prefer readable block style; keep key order from the form merge.
        return yaml.safe_dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    if kind == "handler":
        if not isinstance(data, dict):
            raise ConfigError("handler data must be an object", 400)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if kind == "route":
        if not isinstance(data, list):
            raise ConfigError("routes data must be an array", 400)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    raise ConfigError(f"unknown kind for {rel}", 400)
