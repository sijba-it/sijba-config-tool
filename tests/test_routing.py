"""Routing page: rules ↔ regex-router routes, steps per source, order, templates."""
import json
import re

import pytest
from fastapi.testclient import TestClient

import routing
from test_apply_webhooks import AUTH
from test_connectors import load_app

# The live voice chain (edvbartl-ansible configmap), trimmed.
VOICE_ROUTES = [
    {"pattern": "^(einkauf|einkaufsliste|besorgen)\\b\\s*[:,-]?\\s*", "inject": "@obsidian [einkauf] {{ rest }}"},
    {"pattern": "^(aufgabe|task|todo|to-do)\\b\\s*[:,-]?\\s*", "inject": "@obsidian [task] {{ rest }}"},
    {"pattern": "^(recherche|recherchiere)\\b\\s*[:,-]?\\s*", "inject": "@Recherche {{ rest }}"},
]
TEE = {
    "id": "voice-tee", "priority": 10.0, "direction": "inbound",
    "type": "exec_json", "exec": "{sijba_plugins}/regex-router/main.py",
    "match": {"source": ["webhook:voice"]}, "continue_on_match": True, "timeout_seconds": 10,
    "env": {"ROUTES": "[{\"pattern\":\".*\",\"inject\":\"@obsidian [voice] {{ text }}\"}]",
            "ON_MATCH_CONTINUE": "true"},
}
ROUTER = {
    "id": "router-regex", "priority": 70.0, "direction": "inbound",
    "type": "exec_json", "exec": "{sijba_plugins}/regex-router/main.py",
    "match": {"source": ["webhook:voice"]}, "continue_on_match": False, "timeout_seconds": 10,
    "env": {"ROUTES_FILE": "/data/config/routes/routes-voice.json"},
}
LLM = {
    "id": "router-llm", "priority": 90.0, "direction": "inbound", "type": "exec_json",
    "exec": "{sijba_plugins}/llm-router/main.py", "match": {"source": ["webhook:voice"]},
    "env": {"LLM_MODEL": "openai/gpt-4.1-nano"},
}
ECHO = {"id": "echo", "priority": 99.0, "direction": "inbound", "type": "exec_json", "exec": "{sijba_plugins}/echo.py"}
PUSH = {"id": "pushover", "priority": 50.0, "direction": "outbound", "type": "exec_json", "exec": "x"}


def _write(d, rel, data):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    (tmp_path / "core.yaml").write_text("port: 8080\nwebhooks:\n  voice: tok\n  pebble: tok2\n")
    _write(tmp_path, "routes/routes-voice.json", VOICE_ROUTES)
    for name, h in (("10.0-voice-tee", TEE), ("70.0-router-regex", ROUTER), ("90.0-router-llm", LLM),
                    ("99.0-echo", ECHO), ("50.0-pushover", PUSH)):
        _write(tmp_path, f"handlers/{name}.json", h)
    m = load_app(monkeypatch, tmp_path, SIJBA_AUTH_TOKEN="ui-tok", SIJBA_CORE_URL="http://127.0.0.1:1")
    return TestClient(m.app), tmp_path


def _read(d, rel):
    return json.loads((d / rel).read_text())


# ---- rules ↔ routes

def test_live_routes_read_as_plain_rules():
    rules = [routing.rule_from_route(r) for r in VOICE_ROUTES]
    assert rules[0] == {"when": "starts", "words": ["einkauf", "einkaufsliste", "besorgen"],
                        "target": "@obsidian", "tag": "einkauf", "send": "rest"}
    assert rules[1]["words"] == ["aufgabe", "task", "todo", "to-do"]
    assert rules[2] == {"when": "starts", "words": ["recherche", "recherchiere"],
                        "target": "@Recherche", "tag": "", "send": "rest"}
    # and they are written back byte for byte
    assert [routing.route_from_rule(r) for r in rules] == VOICE_ROUTES


@pytest.mark.parametrize("rule, text, hit, rest", [
    ({"when": "starts", "words": ["merk dir"]}, "Merk  dir: Milch", True, "Milch"),
    ({"when": "starts", "words": ["c++"]}, "c++ lernen", True, "lernen"),
    ({"when": "starts", "words": ["task"]}, "tasks sind toll", False, None),
    ({"when": "contains", "words": ["dringend"]}, "das ist dringend!", True, "!"),
    ({"when": "contains", "words": ["dringend"]}, "dringendst", False, None),
    ({"when": "always"}, "irgendwas", True, ""),
])
def test_generated_patterns_match_like_the_router(rule, text, hit, rest):
    pattern = routing.build_pattern(rule)
    m = re.match(pattern, text, re.I)  # regex-router: re.match + flag i
    assert bool(m) == hit
    if hit:
        assert text[m.end():].strip() == rest
    assert routing.parse_pattern(pattern) == ({"when": "always"} if rule["when"] == "always" else rule)


def test_targets_with_spaces():
    rule = {"when": "always", "target": "@request Tracker", "tag": "bug", "send": "text"}
    route = routing.route_from_rule(rule)
    assert route["inject"] == "@request Tracker [bug] {{ text }}"
    assert routing.rule_from_route(route) == rule
    assert routing.parse_inject("@request Tracker {{ rest }}")["target"] == "@request Tracker"


def test_hand_written_routes_stay_untouched():
    route = {"pattern": "^(?:hey )?(einkauf)\\b", "inject": "@obsidian [{{ 'a' if 'x' in text else 'b' }}] {{ text }}"}
    rule = routing.rule_from_route(route)
    assert rule["when"] == "regex" and rule["target"] == "" and rule["template"] == route["inject"]
    assert routing.route_from_rule(rule) == route


@pytest.mark.parametrize("rule, msg", [
    ({"when": "starts", "words": [" "], "target": "@a"}, "at least one word"),
    ({"when": "regex", "pattern": "(", "target": "@a"}, "invalid regex"),
    ({"when": "always", "target": "obsidian"}, "@name"),
    ({"when": "always", "target": "@a", "tag": "two words"}, "no spaces"),
    ({"when": "always"}, "needs a target"),
])
def test_bad_rules_are_rejected(rule, msg):
    with pytest.raises(routing.ConfigError, match=msg):
        routing.route_from_rule(rule)


# ---- overview

def test_overview_lists_inbound_steps_in_order(cfg):
    c, _ = cfg
    r = c.get("/api/routing", headers=AUTH).json()
    assert [s["id"] for s in r["steps"]] == ["voice-tee", "router-regex", "router-llm", "echo"]
    tee, regex, llm, echo = r["steps"]
    assert tee["kind"] == "rules" and tee["keep_going"] and tee["editable"] and tee["routes_file"] is None
    assert tee["rules"] == [{"when": "always", "target": "@obsidian", "tag": "voice", "send": "text"}]
    assert regex["routes_file"] == "routes/routes-voice.json" and len(regex["rules"]) == 3
    assert llm["kind"] == "ai" and llm["note"] == "openai/gpt-4.1-nano" and not llm["editable"]
    assert echo["kind"] == "other" and echo["sources"] == []
    assert [s["source"] for s in r["sources"]] == ["webhook:voice", "webhook:pebble", "web"]
    # Core is down: targets come from the rules, marked offline
    assert "unreachable" in r["core_error"]
    assert {t["mention"]: t["online"] for t in r["targets"]} == {"@obsidian": False, "@Recherche": False}
    assert {p["id"] for p in r["presets"]} == {"voice-notes", "forward-all"}


def test_targets_come_from_connected_channels():
    channels = [{"channel_id": "cursor:obsidian", "label": "Obsidian", "triggers": ["@obsidian"]},
                {"channel_id": "x", "triggers": []}]
    steps = [{"rules": [{"target": "@Obsidian"}, {"target": "@gone"}]}]
    assert routing.list_targets(channels, steps) == [
        {"mention": "@obsidian", "label": "Obsidian", "channel_id": "cursor:obsidian", "online": True},
        {"mention": "@gone", "label": "@gone", "channel_id": "", "online": False},
    ]


# ---- edit / add / delete

def test_editing_a_file_backed_step_writes_the_routes_file(cfg):
    c, d = cfg
    rules = c.get("/api/routing", headers=AUTH).json()["steps"][1]["rules"]
    rules[0]["words"].append("kaufen")
    rules.pop()
    r = c.put("/api/routing/steps/handlers/70.0-router-regex.json", headers=AUTH,
              json={"sources": ["webhook:voice"], "rules": rules, "keep_going": False})
    assert r.status_code == 200, r.text
    routes = _read(d, "routes/routes-voice.json")
    assert len(routes) == 2 and routes[0]["pattern"].startswith("^(einkauf|einkaufsliste|besorgen|kaufen)")
    h = _read(d, "handlers/70.0-router-regex.json")
    assert h["env"] == {"ROUTES_FILE": "/data/config/routes/routes-voice.json"} and h["continue_on_match"] is False


def test_add_inline_step_then_toggle_keep_going_and_delete(cfg):
    c, d = cfg
    rule = {"when": "starts", "words": ["notiz"], "target": "@obsidian", "tag": "notiz", "send": "rest"}
    r = c.post("/api/routing/steps", headers=AUTH,
               json={"sources": ["webhook:pebble"], "rules": [rule], "priority": 60})
    assert r.status_code == 200, r.text
    step = r.json()
    assert step["file"] == "handlers/60.0-webhook-pebble-rules.json" and step["rules"] == [rule]
    h = _read(d, step["file"])
    assert h["match"] == {"source": ["webhook:pebble"]} and h["exec"] == "{sijba_plugins}/regex-router/main.py"
    assert json.loads(h["env"]["ROUTES"]) == [{"pattern": "^(notiz)\\b\\s*[:,-]?\\s*", "inject": "@obsidian [notiz] {{ rest }}"}]

    r = c.put("/api/routing/steps/" + step["file"], headers=AUTH,
              json={"sources": [], "rules": [rule], "keep_going": True})
    h = _read(d, step["file"])
    assert h["env"]["ON_MATCH_CONTINUE"] == "true" and h["continue_on_match"] is True and "match" not in h

    assert c.delete("/api/routing/steps/" + step["file"], headers=AUTH).status_code == 200
    assert not (d / step["file"]).exists() and (d / (step["file"] + ".bak")).exists()


def test_new_step_by_position_in_the_flow(cfg):
    c, _ = cfg
    rule = {"when": "always", "target": "@obsidian"}
    r = c.post("/api/routing/steps", headers=AUTH, json={"sources": ["webhook:voice"], "rules": [rule], "index": 2})
    assert r.json()["priority"] == 80.0  # between router-regex (70) and router-llm (90)
    r = c.post("/api/routing/steps", headers=AUTH, json={"sources": ["webhook:voice"], "rules": [rule], "index": 0})
    assert r.json()["priority"] == 0.0 and r.json()["id"] == "webhook-voice-rules-2"


def test_only_rule_steps_are_changed_here(cfg):
    c, _ = cfg
    assert c.delete("/api/routing/steps/handlers/90.0-router-llm.json", headers=AUTH).status_code == 422
    r = c.put("/api/routing/steps/handlers/99.0-echo.json", headers=AUTH,
              json={"rules": [{"when": "always", "target": "@a"}]})
    assert r.status_code == 422
    assert c.post("/api/routing/steps", headers=AUTH, json={"rules": []}).status_code == 422


# ---- order

def test_move_renames_between_neighbours(cfg):
    c, d = cfg
    # router-llm before router-regex in the voice flow → between 10 and 70
    r = c.post("/api/routing/move", headers=AUTH,
               json={"source": "webhook:voice", "file": "handlers/90.0-router-llm.json", "index": 1})
    assert r.json() == {"file": "handlers/40.0-router-llm.json", "priority": 40.0}
    assert not (d / "handlers/90.0-router-llm.json").exists()
    assert not (d / "handlers/90.0-router-llm.json.bak").exists()
    assert _read(d, "handlers/40.0-router-llm.json")["priority"] == 40.0
    ids = [s["id"] for s in c.get("/api/routing", headers=AUTH).json()["steps"]]
    assert ids == ["voice-tee", "router-llm", "router-regex", "echo"]

    r = c.post("/api/routing/move", headers=AUTH,
               json={"source": "webhook:voice", "file": "handlers/10.0-voice-tee.json", "index": 9})
    assert r.json()["priority"] == 109.0


@pytest.mark.parametrize("prev, nxt, want", [
    (10, 70, 40), (10, 11, 10.5), (10, 10.1, 10.05), (None, 10, 0), (None, 4, 2), (99, None, 109),
])
def test_between(prev, nxt, want):
    assert routing._between(prev, nxt) == want


# ---- templates

def test_voice_template_with_copy_and_remove(cfg):
    c, d = cfg
    r = c.post("/api/routing/presets/voice-notes", headers=AUTH, json={
        "variant": "copy",
        "params": {"source": "webhook:pebble", "target": "@obsidian", "task": "", "appointment": "termin"}})
    assert r.status_code == 200, r.text
    files = [s["file"] for s in r.json()["steps"]]
    assert files == ["handlers/10.0-webhook-pebble-voice-notes-copy.json",
                     "handlers/70.0-webhook-pebble-voice-notes-keywords.json"]
    copy, keywords = (_read(d, f) for f in files)
    assert copy["category"] == ["preset:voice-notes"] and copy["env"]["ON_MATCH_CONTINUE"] == "true"
    tags = [routing.rule_from_route(x)["tag"] for x in json.loads(keywords["env"]["ROUTES"])]
    assert tags == ["einkauf", "termin"]  # empty task words → no task rule; default shopping words kept

    again = c.post("/api/routing/presets/voice-notes", headers=AUTH,
                   json={"variant": "keywords", "params": {"source": "webhook:pebble", "target": "@obsidian"}})
    assert again.status_code == 409

    flow = [s for s in c.get("/api/routing", headers=AUTH).json()["steps"] if s["preset"] == "voice-notes"]
    assert len(flow) == 2

    r = c.delete("/api/routing/presets/voice-notes", params={"source": "webhook:pebble"}, headers=AUTH)
    assert sorted(r.json()["removed"]) == sorted(files)
    assert c.delete("/api/routing/presets/voice-notes", params={"source": "webhook:pebble"},
                    headers=AUTH).status_code == 404


def test_forward_all_template_needs_source_and_target(cfg):
    c, d = cfg
    r = c.post("/api/routing/presets/forward-all", headers=AUTH, json={"variant": "forward", "params": {"source": "web"}})
    assert r.status_code == 422
    r = c.post("/api/routing/presets/forward-all", headers=AUTH,
               json={"variant": "forward", "params": {"source": "web", "target": "@agent"}})
    h = _read(d, r.json()["steps"][0]["file"])
    assert h["id"] == "web-forward-all-forward" and h["continue_on_match"] is False
    assert json.loads(h["env"]["ROUTES"]) == [{"pattern": ".*", "inject": "@agent {{ text }}"}]
    assert c.post("/api/routing/presets/nope", headers=AUTH, json={"variant": "x"}).status_code == 404


def test_routing_needs_auth(cfg):
    c, _ = cfg
    assert c.get("/api/routing").status_code == 401
