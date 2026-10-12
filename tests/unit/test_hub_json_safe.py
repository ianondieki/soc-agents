"""Every frame the realtime hub keeps is plain JSON (realtime.hub.json_safe).

A producer once handed the hub a payload with datetime values (a maintenance window change).
The socket failed to encode that frame, closed, and every reconnect replayed it, so every
console stayed "Reconnecting" until fifteen newer events pushed it out. Normalising once, at
publish, means no producer can take the live feed down again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.services.clock import z_utc


def test_a_payload_with_datetimes_and_sets_is_kept_as_plain_json():
    hub._history.clear()
    naive = datetime(2026, 10, 12, 9, 30, 15, 123456)
    hub.publish_sync(
        RealtimeEvent(
            type="maintenance.window",
            operator_id="safaricom",
            payload={
                "starts_at": z_utc(naive),
                "ends_at": naive,
                "aware": datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
                "tags": {"b", "a"},
                "nested": {"at": naive},
            },
        )
    )
    (frame,) = hub.recent(1)
    json.dumps(frame)  # what send_json does: must not raise
    payload = frame["payload"]
    assert payload["starts_at"] == payload["ends_at"] == "2026-10-12T09:30:15Z"
    assert payload["aware"] == "2026-10-12T12:00:00Z"
    assert payload["tags"] == ["a", "b"]
    assert payload["nested"]["at"] == "2026-10-12T09:30:15Z"
    hub._history.clear()


def test_a_plain_payload_is_unchanged():
    hub._history.clear()
    body = {"incident_number": "INC000001", "count": 3, "ok": True, "none": None, "list": [1, "x"]}
    hub.publish_sync(RealtimeEvent(type="incident.note", operator_id="safaricom", payload=dict(body)))
    assert hub.recent(1)[0]["payload"] == body
    hub._history.clear()
