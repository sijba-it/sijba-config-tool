"""Routing view: inbound handlers as an ordered flow of steps per source.

Steps backed by the regex-router plugin are editable as plain rules
("starts with einkauf → @obsidian [einkauf], the text after the keyword");
this module turns rules into regex-router routes and back. A route that does
not round-trip exactly is shown as an expert rule (raw regex / template), so
nothing written by hand is ever rewritten. Other handlers (LLM router,
plugins) are listed in their place but edited under Advanced.

Order is priority: moving a step gives it a priority between its new
neighbours and renames the file to match (`NN.N-id.json`).
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from codec import serialize_content
from fs_backend import ConfigError, FsBackend

REGEX_ROUTER = "regex-router/main.py"
LLM_ROUTER = "llm-router/main.py"
REGEX_ROUTER_EXEC = "{sijba_plugins}/" + REGEX_ROUTER
DEFAULT_PRIORITY = 50.0  # Core's default when neither the file nor its name has one
PRESET_CATEGORY = "preset:"
_TRUTHY = {"1", "true", "yes", "on"}

# ---------------------------------------------------------------- rules ↔ routes

# `\b` only works next to a word character; a word like "c++" gets a lookaround.
_SEP = r"\s*[:,-]?\s*"
_END = (r"\b", r"(?!\w)")
_START = (r"\b", r"(?<!\w)")
_SPECIAL = re.compile(r"([.*+?^${}()|\[\]\\])")
_TARGET = re.compile(r"^@[^\s\[\]{}]+$")
_TAG = re.compile(r"^[^\s\[\]{}]+$")
_INJECT = re.compile(r"(@[^\s\[\]{}]+)(?: \[([^\s\[\]{}]+)\])? \{\{ (rest|text) \}\}")


def _esc_word(word: str) -> str:
    return r"\s+".join(_SPECIAL.sub(r"\\\1", part) for part in word.split())


def _unesc_word(s: str) -> str:
    return " ".join(re.sub(r"\\(.)", r"\1", part) for part in s.split(r"\s+"))


def _alternation(words: list[str]) -> str:
    return "|".join(_esc_word(w) for w in words)


def _words(s: str) -> list[str]:
    return [_unesc_word(p) for p in re.split(r"(?<!\\)\|", s)]


def build_pattern(rule: dict) -> str:
    when = rule.get("when")
    if when == "always":
        return ".*"
    if when == "regex":
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            raise ConfigError("expert rule: the regex is empty", 422)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex {pattern!r}: {exc}", 422) from exc
        return pattern
    words = [w.strip() for w in rule.get("words") or [] if str(w).strip()]
    if not words:
        raise ConfigError("a rule needs at least one word", 422)
    end = _END[0] if all(re.match(r"\w", w[-1]) for w in words) else _END[1]
    if when == "starts":
        return "^(" + _alternation(words) + ")" + end + _SEP
    if when == "contains":
        start = _START[0] if all(re.match(r"\w", w[0]) for w in words) else _START[1]
        return "^.*?" + start + "(" + _alternation(words) + ")" + end
    raise ConfigError(f"unknown condition {when!r}", 422)


def parse_pattern(pattern: str) -> dict:
    if pattern == ".*":
        return {"when": "always"}
    candidates = []
    end = "(?:" + "|".join(map(re.escape, _END)) + ")"
    start = "(?:" + "|".join(map(re.escape, _START)) + ")"
    m = re.fullmatch(r"\^\((.+)\)" + end + re.escape(_SEP), pattern)
    if m:
        candidates.append({"when": "starts", "words": _words(m.group(1))})
    m = re.fullmatch(re.escape("^.*?") + start + r"\((.+)\)" + end, pattern)
    if m:
        candidates.append({"when": "contains", "words": _words(m.group(1))})
    for rule in candidates:
        try:
            if build_pattern(rule) == pattern:
                return rule
        except ConfigError:
            pass
    return {"when": "regex", "pattern": pattern}


def build_inject(rule: dict) -> str:
    target = str(rule.get("target") or "").strip()
    if not target:
        template = str(rule.get("template") or "").strip()
        if not template:
            raise ConfigError("a rule needs a target", 422)
        return template
    if not _TARGET.match(target):
        raise ConfigError(f"target must look like @name, got {target!r}", 422)
    tag = str(rule.get("tag") or "").strip()
    if tag and not _TAG.match(tag):
        raise ConfigError(f"label {tag!r}: no spaces or brackets", 422)
    send = rule.get("send") or "rest"
    if send not in ("rest", "text"):
        raise ConfigError(f"unknown text choice {send!r}", 422)
    return target + (f" [{tag}]" if tag else "") + " {{ " + send + " }}"


def parse_inject(template: str) -> dict:
    m = _INJECT.fullmatch(template)
    if m:
        rule = {"target": m.group(1), "tag": m.group(2) or "", "send": m.group(3)}
        if build_inject(rule) == template:
            return rule
    return {"target": "", "template": template}


def rule_from_route(route: dict) -> dict:
    return {**parse_pattern(str(route.get("pattern") or "")),
            **parse_inject(str(route.get("inject") or ""))}


def route_from_rule(rule: dict) -> dict:
    return {"pattern": build_pattern(rule), "inject": build_inject(rule)}


# ---------------------------------------------------------------- handlers → steps

def _priority(handler: dict, filename: str) -> float:
    if isinstance(handler.get("priority"), (int, float)):
        return float(handler["priority"])
    m = re.match(r"^(\d+(?:\.\d+)?)", filename)
    return float(m.group(1)) if m else DEFAULT_PRIORITY


def _kind(handler: dict) -> str:
    exe = str(handler.get("exec") or "")
    if exe.endswith(REGEX_ROUTER):
        return "rules"
    if exe.endswith(LLM_ROUTER):
        return "ai"
    return "other"


def _routes_rel(path: str) -> str | None:
    """Map a ROUTES_FILE path (as Core sees it) to a config-relative path."""
    p = PurePosixPath(path)
    if p.parent.name == "routes" and p.suffix == ".json":
        return f"routes/{p.name}"
    return None


def _read_json(backend: FsBackend, rel: str) -> Any:
    return json.loads(backend.get(rel)["content"])


def _load_routes(backend: FsBackend, env: dict) -> tuple[list | None, str | None, str]:
    """(routes, routes_file_rel, note). routes is None when they can't be edited here."""
    routes_file = str(env.get("ROUTES_FILE") or "").strip()
    if routes_file:
        rel = _routes_rel(routes_file)
        if not rel:
            return None, None, f"routes come from {routes_file}, outside the config folder"
        try:
            data = _read_json(backend, rel)
        except (ConfigError, json.JSONDecodeError) as exc:
            return None, rel, f"cannot read {rel}: {exc}"
        if not isinstance(data, list):
            return None, rel, f"{rel} is not a list"
        return data, rel, ""
    try:
        data = json.loads(env.get("ROUTES") or "[]")
    except json.JSONDecodeError as exc:
        return None, None, f"ROUTES is not valid JSON: {exc}"
    if not isinstance(data, list):
        return None, None, "ROUTES is not a list"
    return data, None, ""


def _step(backend: FsBackend, rel: str, handler: dict) -> dict:
    name = PurePosixPath(rel).name
    env = handler.get("env") if isinstance(handler.get("env"), dict) else {}
    match = handler.get("match") if isinstance(handler.get("match"), dict) else {}
    presets = [c[len(PRESET_CATEGORY):] for c in handler.get("category") or []
               if isinstance(c, str) and c.startswith(PRESET_CATEGORY)]
    step: dict[str, Any] = {
        "file": rel,
        "id": handler.get("id") or name,
        "priority": _priority(handler, name),
        "sources": list(match.get("source") or []),
        "only_matching": bool(match.get("regex")),
        "kind": _kind(handler),
        "preset": presets[0] if presets else None,
        "editable": False,
        "note": "",
    }
    if step["kind"] == "rules":
        routes, routes_file, note = _load_routes(backend, env)
        step["keep_going"] = str(env.get("ON_MATCH_CONTINUE", "")).strip().lower() in _TRUTHY
        step["routes_file"] = routes_file
        step["note"] = note
        if routes is not None:
            step["rules"] = [rule_from_route(r) for r in routes if isinstance(r, dict)]
            step["editable"] = True
    elif step["kind"] == "ai":
        step["note"] = str(env.get("LLM_MODEL") or "")
    else:
        step["note"] = PurePosixPath(str(handler.get("exec") or handler.get("type") or "")).name
    return step


def _handler_files(backend: FsBackend) -> list[str]:
    return [f["path"] for f in backend.list_files() if f["kind"] == "handler"]


def load_steps(backend: FsBackend) -> tuple[list[dict], list[str]]:
    """Inbound handlers in Core's order (priority, then filename) + unreadable files."""
    steps, problems = [], []
    for rel in _handler_files(backend):
        try:
            handler = _read_json(backend, rel)
        except (ConfigError, json.JSONDecodeError) as exc:
            problems.append(f"{rel}: {exc}")
            continue
        if not isinstance(handler, dict):
            problems.append(f"{rel}: not a JSON object")
            continue
        if handler.get("direction", "inbound") != "inbound":
            continue
        steps.append(_step(backend, rel, handler))
    steps.sort(key=lambda s: (s["priority"], s["file"]))
    return steps, problems


def flow(steps: list[dict], source: str) -> list[dict]:
    """The steps a message from `source` passes through, in order."""
    return [s for s in steps if not s["sources"] or source in s["sources"]]


# ---------------------------------------------------------------- writing

def _fmt_priority(p: float) -> str:
    return f"{p:.1f}" if float(p).is_integer() else f"{p:g}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "all"


def _filename(priority: float, handler_id: str) -> str:
    return f"handlers/{_fmt_priority(priority)}-{handler_id}.json"


def _existing_ids(backend: FsBackend) -> set[str]:
    out = set()
    for rel in _handler_files(backend):
        try:
            h = _read_json(backend, rel)
        except (ConfigError, json.JSONDecodeError):
            continue
        if isinstance(h, dict) and h.get("id"):
            out.add(str(h["id"]))
    return out


def _unique_id(backend: FsBackend, base: str) -> str:
    taken = _existing_ids(backend)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _new_handler(handler_id: str, priority: float) -> dict:
    return {
        "id": handler_id,
        "priority": priority,
        "direction": "inbound",
        "lifecycle": "per_message",
        "type": "exec_json",
        "exec": REGEX_ROUTER_EXEC,
        "timeout_seconds": 10,
    }


def _clean_sources(sources: Any) -> list[str]:
    return [str(s).strip() for s in sources or [] if str(s).strip()]


def save_step(backend: FsBackend, payload: dict, *, category: str | None = None,
              handler_id: str | None = None) -> dict:
    """Create (no `file`) or update a rules step from {sources, rules, keep_going}.
    A new step is placed by `priority`, or by `index` in the flow of its first source."""
    rules = payload.get("rules") or []
    if not rules:
        raise ConfigError("add at least one rule", 422)
    routes = [route_from_rule(r) for r in rules]
    keep_going = bool(payload.get("keep_going"))
    sources = _clean_sources(payload.get("sources"))

    rel = payload.get("file")
    if rel:
        handler = _read_json(backend, rel)
        if not isinstance(handler, dict) or _kind(handler) != "rules":
            raise ConfigError(f"{rel} is not a rules step", 422)
        env = dict(handler.get("env") or {})
        _routes, routes_file, note = _load_routes(backend, env)
        if _routes is None:
            raise ConfigError(note, 422)
    else:
        priority = payload.get("priority")
        if priority is None and payload.get("index") is not None:
            # "put it at position N of the flow" — same placement as move_step
            steps, _ = load_steps(backend)
            others = flow(steps, sources[0]) if sources else steps
            i = max(0, min(int(payload["index"]), len(others)))
            priority = _between(others[i - 1]["priority"] if i > 0 else None,
                                others[i]["priority"] if i < len(others) else None)
            if priority is None:
                priority = DEFAULT_PRIORITY
        if not isinstance(priority, (int, float)) or priority < 0:
            raise ConfigError("a new step needs a priority or a position", 422)
        base = handler_id or f"{_slug(sources[0] if sources else 'all')}-{'copy' if keep_going else 'rules'}"
        hid = handler_id or _unique_id(backend, base)
        rel = _filename(float(priority), hid)
        if backend.resolve(rel).exists():
            raise ConfigError(f"{rel} already exists", 409)
        handler = _new_handler(hid, float(priority))
        env, routes_file = {}, None

    if routes_file:
        backend.put(routes_file, serialize_content(routes_file, routes), backup=True)
    else:
        env["ROUTES"] = json.dumps(routes, ensure_ascii=False, separators=(",", ":"))
    if keep_going:
        env["ON_MATCH_CONTINUE"] = "true"
    else:
        env.pop("ON_MATCH_CONTINUE", None)
    handler["env"] = env
    handler["continue_on_match"] = keep_going

    match = dict(handler.get("match") or {})
    if sources:
        match["source"] = sources
    else:
        match.pop("source", None)
    if match:
        handler["match"] = match
    else:
        handler.pop("match", None)

    if category:
        cats = [c for c in handler.get("category") or [] if c != category]
        handler["category"] = cats + [category]

    backend.put(rel, serialize_content(rel, handler), backup=True)
    return _step(backend, rel, handler)


def delete_step(backend: FsBackend, rel: str) -> None:
    handler = _read_json(backend, rel)
    if not isinstance(handler, dict) or _kind(handler) != "rules":
        raise ConfigError("only rule steps can be removed here — use Advanced", 422)
    backend.delete(rel)


def _between(prev: float | None, nxt: float | None) -> float | None:
    if prev is None and nxt is None:
        return None
    if prev is None:
        return nxt - 10 if nxt >= 10 else nxt / 2
    if nxt is None:
        return prev + 10
    for digits in (0, 1, 2):
        mid = round((prev + nxt) / 2, digits)
        if prev < mid < nxt:
            return mid
    return (prev + nxt) / 2


def move_step(backend: FsBackend, source: str, rel: str, index: int) -> dict:
    """Move `rel` to position `index` in the flow of `source`."""
    steps, _ = load_steps(backend)
    others = [s for s in flow(steps, source) if s["file"] != rel]
    if not any(s["file"] == rel for s in steps):
        raise ConfigError(f"not found: {rel}", 404)
    index = max(0, min(index, len(others)))
    prev = others[index - 1]["priority"] if index > 0 else None
    nxt = others[index]["priority"] if index < len(others) else None
    priority = _between(prev, nxt)
    if priority is None:
        return {"file": rel}

    handler = _read_json(backend, rel)
    handler["priority"] = priority
    rest = re.sub(r"^\d+(\.\d+)?-", "", PurePosixPath(rel).name)
    new_rel = f"handlers/{_fmt_priority(priority)}-{rest}"
    if new_rel != rel and backend.resolve(new_rel).exists():
        raise ConfigError(f"{new_rel} already exists", 409)
    backend.put(new_rel, serialize_content(new_rel, handler), backup=new_rel == rel)
    if new_rel != rel:
        backend.delete(rel, backup=False)
    return {"file": new_rel, "priority": priority}


# ---------------------------------------------------------------- sources / targets

SOURCE_LABELS = {"web": "sijba app", "telegram": "Telegram"}


def source_label(source: str) -> str:
    if source.startswith("webhook:"):
        return "Webhook · " + source[len("webhook:"):]
    return SOURCE_LABELS.get(source, source)


def list_sources(webhook_names: list[str], steps: list[dict]) -> list[dict]:
    seen: list[str] = [f"webhook:{n}" for n in webhook_names]
    for s in steps:
        for src in s["sources"]:
            if src not in seen:
                seen.append(src)
    if "web" not in seen:
        seen.append("web")
    return [{"source": s, "label": source_label(s)} for s in seen]


def list_targets(channels: list[dict], steps: list[dict]) -> list[dict]:
    """Connected plugin channels by @mention, plus targets rules use that are not
    connected. Core matches mentions case-insensitively, so this does too."""
    out, seen = [], set()
    for ch in channels:
        triggers = ch.get("triggers") or []
        if not triggers:
            continue
        mention = str(triggers[0])
        if mention.lower() in seen:
            continue
        seen.add(mention.lower())
        out.append({"mention": mention, "label": ch.get("label") or mention,
                    "channel_id": ch.get("channel_id", ""), "online": True})
    for s in steps:
        for r in s.get("rules") or []:
            t = r.get("target")
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append({"mention": t, "label": t, "channel_id": "", "online": False})
    return out
