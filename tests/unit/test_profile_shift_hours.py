"""The Shift desk draws the current shift's window from ``shift_hours`` on /api/v1/profile.

The route is unauthenticated, so it carries each shift's start and end and nothing else: the
distribution list each shift has in the operator config never reaches it.
"""

import re

from fastapi.testclient import TestClient

from noc_agents.main import app


def test_profile_serves_shift_hours_and_only_the_times():
    client = TestClient(app)
    body = client.get("/api/v1/profile").json()
    hours = body["shift_hours"]
    assert set(hours) >= {"day", "night"}
    for window in hours.values():
        assert set(window) == {"start", "end"}
        assert re.fullmatch(r"\d{2}:\d{2}", window["start"])
        assert re.fullmatch(r"\d{2}:\d{2}", window["end"])
    assert "@" not in client.get("/api/v1/profile").text
