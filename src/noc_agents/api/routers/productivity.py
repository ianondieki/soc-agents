"""``GET /api/v1/metrics/productivity`` — what the agents did, and what it saved.

Thin by design, like ``dashboards``: open a session, call
``services.productivity.productivity``, close the session. All the arithmetic and every
operator-scoped query live in the service so they can be tested without an HTTP client.

Read-only: nothing here writes, enqueues or sends. Gated with ``PLATFORM_READERS``, §9.3's
platform row read cell, because the payload describes the system's own shape and throughput
(every agent, its steps, its timings, the toil model) rather than any one incident — the
same reasoning as ``GET /api/v1/agents``. Inert while ``AUTH_DISABLED=true``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from noc_agents.api.auth import require_role
from noc_agents.api.deps import PLATFORM_READERS, _settings
from noc_agents.db.models import get_session
from noc_agents.services.productivity import DEFAULT_WINDOW_HOURS, MAX_WINDOW_HOURS, productivity

router = APIRouter(prefix="/api/v1", tags=["metrics"])


@router.get("/metrics/productivity", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def metrics_productivity(
    window_hours: int = Query(
        DEFAULT_WINDOW_HOURS,
        ge=0,
        le=MAX_WINDOW_HOURS,
        description="Hours back from now to roll up; 0 = everything on record.",
    ),
) -> dict[str, Any]:
    """Agent throughput, the approvals asked of humans and the toil-minutes model, per operator.

    The response shape is pinned by ``tests/unit/test_productivity.py``: adding a key is fine,
    renaming or removing one breaks the Showcase page.
    """
    session = get_session()
    try:
        return productivity(session, _settings().operator, window_hours=window_hours)
    finally:
        session.close()
