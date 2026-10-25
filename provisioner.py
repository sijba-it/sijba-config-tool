"""Thin client for the provisioner API (PROVISIONER_URL + PROVISIONER_TOKEN)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ProvisionerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Provisioner:
    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read()).get("error") or exc.reason
            except Exception:
                msg = exc.reason
            raise ProvisionerError(exc.code, str(msg)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProvisionerError(502, f"provisioner unreachable: {getattr(exc, 'reason', exc)}") from exc
        return json.loads(raw) if raw else {}

    def catalog(self) -> dict:
        return self._call("GET", "/connectors/catalog")

    def list(self) -> dict:
        return self._call("GET", "/connectors")

    def put(self, ctype: str, fields: dict) -> dict:
        return self._call("PUT", "/connectors/" + urllib.parse.quote(ctype, safe=""), {"fields": fields})

    def delete(self, ctype: str) -> dict:
        return self._call("DELETE", "/connectors/" + urllib.parse.quote(ctype, safe=""))
