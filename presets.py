"""Routing templates: ready-made sets of rule steps, filled in with a few answers.

Each template has parameters (where messages come from, where they go, the
keywords) and variants. Applying one writes ordinary rule steps, tagged with
the category `preset:<id>` so the steps can be recognised and removed as a set.
"""
from __future__ import annotations

from typing import Any

from fs_backend import ConfigError, FsBackend
from routing import PRESET_CATEGORY, _clean_sources, _existing_ids, _slug, source_label, flow, load_steps, save_step

PRESETS: list[dict[str, Any]] = [
    {
        "id": "voice-notes",
        "label": "Voice notes → notebook",
        "description": "A note that starts with a keyword is filed as a shopping item, task or appointment.",
        "params": [
            {"name": "source", "label": "Messages from", "kind": "source", "default": "webhook:voice"},
            {"name": "target", "label": "Notebook", "kind": "target", "default": "@obsidian"},
            {"name": "shopping", "label": "Shopping words", "kind": "words",
             "default": "einkauf, einkaufsliste, einkaufszettel, einkaufen, besorgen, kaufen"},
            {"name": "task", "label": "Task words", "kind": "words", "default": "aufgabe, task, todo, to-do"},
            {"name": "appointment", "label": "Appointment words", "kind": "words", "default": "termin, kalender"},
        ],
        "variants": [
            {"id": "keywords", "label": "Keywords only",
             "description": "Only notes that start with one of the words are filed."},
            {"id": "copy", "label": "Keywords + keep every note",
             "description": "Every note is also sent in full, labelled [voice], before the keywords are checked."},
        ],
    },
    {
        "id": "forward-all",
        "label": "Everything to one place",
        "description": "Every message from one source goes to one agent or app.",
        "params": [
            {"name": "source", "label": "Messages from", "kind": "source", "default": ""},
            {"name": "target", "label": "Send to", "kind": "target", "default": ""},
            {"name": "tag", "label": "Label (optional)", "kind": "text", "default": ""},
        ],
        "variants": [
            {"id": "forward", "label": "Forward",
             "description": "Later steps don't see the message any more."},
            {"id": "copy", "label": "Copy",
             "description": "Later steps still run, e.g. keyword rules."},
        ],
    },
]


def _words(value: Any) -> list[str]:
    return [w.strip() for w in str(value or "").split(",") if w.strip()]


def _starts(words: list[str], target: str, tag: str) -> dict:
    return {"when": "starts", "words": words, "target": target, "tag": tag, "send": "rest"}


def _always(target: str, tag: str) -> dict:
    return {"when": "always", "target": target, "tag": tag, "send": "text"}


def _build(preset_id: str, variant: str, p: dict) -> list[dict]:
    """[{name, priority, keep_going, rules}] for one template + variant."""
    target = p["target"]
    if preset_id == "voice-notes":
        rules = [_starts(_words(p.get(key)), target, tag)
                 for key, tag in (("shopping", "einkauf"), ("task", "task"), ("appointment", "termin"))
                 if _words(p.get(key))]
        if not rules:
            raise ConfigError("enter at least one keyword", 422)
        steps = [{"name": "keywords", "priority": 70.0, "keep_going": False, "rules": rules}]
        if variant == "copy":
            steps.insert(0, {"name": "copy", "priority": 10.0, "keep_going": True,
                             "rules": [_always(target, "voice")]})
        return steps
    if preset_id == "forward-all":
        return [{"name": variant, "priority": 80.0, "keep_going": variant == "copy",
                 "rules": [_always(target, str(p.get("tag") or "").strip())]}]
    raise ConfigError(f"unknown template {preset_id!r}", 404)


def _preset(preset_id: str) -> dict:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    raise ConfigError(f"unknown template {preset_id!r}", 404)


def apply_preset(backend: FsBackend, preset_id: str, variant: str, params: dict) -> list[dict]:
    preset = _preset(preset_id)
    if variant not in {v["id"] for v in preset["variants"]}:
        raise ConfigError(f"unknown variant {variant!r}", 422)
    values = {p["name"]: params.get(p["name"], p["default"]) for p in preset["params"]}
    source = str(values.get("source") or "").strip()
    target = str(values.get("target") or "").strip()
    if not source:
        raise ConfigError("choose where the messages come from", 422)
    if not target:
        raise ConfigError("choose where the messages go", 422)
    values.update(source=source, target=target)

    taken = _existing_ids(backend)
    planned = []
    for step in _build(preset_id, variant, values):
        hid = f"{_slug(source)}-{preset_id}-{step['name']}"
        if hid in taken:
            raise ConfigError(f"this template is already set up for {source_label(source)}", 409)
        planned.append((hid, step))
    return [
        save_step(backend, {"sources": [source], "rules": step["rules"], "keep_going": step["keep_going"],
                            "priority": step["priority"]},
                  category=PRESET_CATEGORY + preset_id, handler_id=hid)
        for hid, step in planned
    ]


def remove_preset(backend: FsBackend, preset_id: str, source: str) -> list[str]:
    """Remove the rule steps a template created for `source`."""
    steps, _ = load_steps(backend)
    removed = []
    for s in flow(steps, source):
        if s["preset"] == preset_id and s["kind"] == "rules" and _clean_sources(s["sources"]) == [source]:
            backend.delete(s["file"])
            removed.append(s["file"])
    if not removed:
        raise ConfigError("nothing from this template here", 404)
    return removed
