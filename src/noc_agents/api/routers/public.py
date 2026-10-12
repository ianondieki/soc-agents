"""``GET /api/v1/public/status``: the customer's service status page.

Open on purpose (no ``require_role``), like the complaint form and the Track page: a customer
reads it before they have, or need, anything else. ``services.public_status`` decides what a
customer may know (region states, services, counties, planned works) and keeps everything else
off the payload. Read-only, and cheap: a few aggregate queries, cached by the browser and any
proxy for ``CACHE_SECONDS`` so a page left open on many phones does not become load.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from noc_agents.db.models import get_session
from noc_agents.services.public_status import public_status

router = APIRouter(prefix="/api/v1/public", tags=["public"])

#: How long a reader may reuse an answer. The page itself refreshes every minute.
CACHE_SECONDS = 30


@router.get("/status")
def service_status(response: Response) -> dict[str, Any]:
    """Each region's state, the services affected, and planned works in the next seven days.

    The response shape is pinned by ``tests/unit/test_public_status.py``.
    """
    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    session = get_session()
    try:
        return public_status(session)
    finally:
        session.close()
