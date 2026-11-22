"""Which agents (plugin channels) may send messages to other channels.

An agent that is allowed to answers with a fenced block in its reply:

    ```sijba-inject <channel_id>
    text for the other channel
    ```

Core delivers the text to <channel_id> as a new message. The permission (`can_inject`)
lives in Core (`channel_permissions.yaml`, set through its admin API) - it is checked
on every message and never decided by the agent itself, so this module only reads and
writes it through Core.
"""
from __future__ import annotations

from typing import Any

FORMAT_EXAMPLE = "```sijba-inject <channel id>\ntext for the other channel\n```"


def overview(channels: list[dict], permissions: dict[str, Any]) -> list[dict]:
    """Connected agents first (in the order Core lists them), then agents that have a
    permission entry but are not connected right now. `permissions` is Core's
    `{channel_id: {can_inject: bool}}` mapping."""
    out, seen = [], set()
    for ch in channels:
        cid = ch.get("channel_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        triggers = ch.get("triggers") or []
        out.append({"channel_id": cid, "label": ch.get("label") or cid,
                    "mention": str(triggers[0]) if triggers else "", "online": True,
                    "can_inject": bool((permissions.get(cid) or {}).get("can_inject"))})
    for cid, flags in sorted(permissions.items()):
        if cid not in seen:
            out.append({"channel_id": cid, "label": cid, "mention": "", "online": False,
                        "can_inject": bool((flags or {}).get("can_inject"))})
    return out
