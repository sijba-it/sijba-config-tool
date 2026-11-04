"""Change history for the config folder.

Every write or delete through FsBackend is recorded as a change: the file's
content before and after, stored once per content hash under
`<config>/.history/` (`log.jsonl` + `blobs/`). Related writes — a template
adding three files, a move that renames one — are grouped into one change with
`with history.change("label"):`. A change can be undone, which itself is
recorded as a new change. The folder starts with a dot, so the file API never
exposes it and Core never loads it.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

MAX_CHANGES = 300


class History:
    def __init__(self, root: Path, max_changes: int = MAX_CHANGES):
        self.root = root
        self.max_changes = max_changes
        self._open: Optional[list[dict]] = None
        self._last_id = 0

    # -- writing

    @contextmanager
    def change(self, label: str) -> Iterator[None]:
        """Group every write inside into one change. Nested groups join the outer one."""
        if self._open is not None:
            yield
            return
        self._open = []
        try:
            yield
        finally:
            ops, self._open = self._open, None
            if ops:
                self._commit(label, ops)

    def record(self, rel: str, before: Optional[str], after: Optional[str]) -> None:
        if before == after:
            return
        op = {"path": rel, "before": self._blob(before), "after": self._blob(after)}
        if self._open is None:
            verb = "Created" if before is None else "Deleted" if after is None else "Saved"
            self._commit(f"{verb} {rel}", [op])
            return
        for prev in self._open:
            if prev["path"] == rel:
                prev["after"] = op["after"]
                if prev["before"] == prev["after"]:
                    self._open.remove(prev)
                return
        self._open.append(op)

    def _blob(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        sha = hashlib.sha256(text.encode()).hexdigest()[:24]
        path = self.root / "blobs" / sha
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return sha

    def _commit(self, label: str, ops: list[dict]) -> None:
        now = int(time.time() * 1000)
        self._last_id = max(now, self._last_id + 1)
        entry = {
            "id": str(self._last_id),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": label,
            "files": ops,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._prune()

    def _prune(self) -> None:
        entries = self._entries()
        if len(entries) <= self.max_changes:
            return
        keep = entries[-self.max_changes:]
        tmp = self.root / "log.jsonl.tmp"
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep), encoding="utf-8")
        tmp.replace(self.root / "log.jsonl")
        used = {f[k] for e in keep for f in e["files"] for k in ("before", "after") if f[k]}
        for blob in (self.root / "blobs").glob("*"):
            if blob.name not in used:
                blob.unlink()

    # -- reading

    def _entries(self) -> list[dict]:
        path = self.root / "log.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line after a crash
        return out

    def _text(self, sha: Optional[str]) -> Optional[str]:
        if sha is None:
            return None
        path = self.root / "blobs" / sha
        return path.read_text(encoding="utf-8") if path.is_file() else None

    @staticmethod
    def _action(f: dict) -> str:
        return "added" if f["before"] is None else "deleted" if f["after"] is None else "changed"

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, without file contents."""
        return [
            {"id": e["id"], "ts": e["ts"], "label": e["label"],
             "files": [{"path": f["path"], "action": self._action(f)} for f in e["files"]]}
            for e in reversed(self._entries()[-limit:])
        ]

    def get(self, change_id: str, current: dict[str, Optional[str]]) -> dict[str, Any]:
        """One change with before/after contents. `current` maps path → content now,
        to flag files edited again since (undoing then also drops those edits)."""
        for e in self._entries():
            if e["id"] == change_id:
                files = []
                for f in e["files"]:
                    after = self._text(f["after"])
                    files.append({"path": f["path"], "action": self._action(f),
                                  "before": self._text(f["before"]), "after": after,
                                  "changed_since": current.get(f["path"]) != after})
                return {"id": e["id"], "ts": e["ts"], "label": e["label"], "files": files}
        raise KeyError(change_id)
