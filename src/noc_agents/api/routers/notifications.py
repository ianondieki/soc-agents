"""``GET /api/v1/notifications`` — the notification centre's inbox (the bell in the top bar).

Thin by design, like ``dashboards`` and ``productivity``: open a session, call
``services.notifications.notifications``, close the session. What counts as attention, the
operator scoping and the per-role filter of approval cards all live in the service.

Read-only: nothing here writes, enqueues or sends; what a person has read is kept in their own
browser. Gated with ``INCIDENT_READERS``, §9.3 row 1's read column, because every item names a
ticket, its site and its region. Approval cards are filtered further to the kinds the signed-in
role may decide, the same rule as ``/api/v1/hitl/pending``. Inert while ``AUTH_DISABLED=true``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import INCIDENT_READERS
from noc_agents.db.models import get_session
from noc_agents.services.notifications import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_HOURS,
    MAX_LIMIT,
    MAX_WINDOW_HOURS,
    notifications,
)

router = APIRouter(prefix="/api/v1", tags=["notifications"])


@router.get("/notifications")
def list_notifications(
    window_hours: int = Query(DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS, description="Hours back from now."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Most items to return, newest first."),
    principal: auth.Principal = Depends(require_role(*INCIDENT_READERS)),
) -> dict[str, Any]:
    """Open P1s, restore clocks run out, cards waiting for a decision and failed agent runs.

    The response shape is pinned by ``tests/unit/test_notifications.py``.
    """
    session = get_session()
    try:
        return notifications(
            session,
            role=principal.role,
            authenticated=principal.authenticated,
            window_hours=window_hours,
            limit=limit,
        )
    finally:
        session.close()
