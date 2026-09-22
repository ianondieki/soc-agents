"""Dashboard read surfaces (Phase 4). Today: the Regions dashboard of spec §7.4.2.

Thin by design. The route opens a session, hands it to
``services.dashboards.regions_dashboard`` and closes it in a ``finally`` — the same
idiom ``GET /api/v1/metrics/summary`` uses in ``main.py``. All the rollup logic,
and every operator-scoped query behind it, lives in the service so it can be
tested without an HTTP client and so this file stays reviewable at a glance.

Read-only: nothing here writes, enqueues or sends. Gated with ``INCIDENT_READERS``,
§9.3 row 1's read column: the payload carries problem records (numbers, sites, known-error
flags) and signal freshness, which are row-1 reads, so legal holds R and the two vendor
roles ("notes only") do not. This used to be ``READERS`` on the reasoning that a field
engineer and an MSP coordinator have business looking at where the network hurts tonight;
that is now an owner question, not a gate. Inert while ``AUTH_DISABLED=true``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from noc_agents.api.auth import require_role
from noc_agents.api.deps import INCIDENT_READERS
from noc_agents.db.models import get_session
from noc_agents.services.dashboards import regions_dashboard

router = APIRouter(prefix="/api/v1", tags=["dashboards"])


@router.get("/dashboard/regions", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def dashboard_regions() -> dict[str, Any]:
    """Per-region fault picture, signal freshness and regulatory baseline (§7.4.2).

    The response shape is pinned by ``tests/unit/test_dashboard_regions.py`` — it is
    the Phase 4 exit criterion "dashboard contract test", and the frontend is
    entitled to depend on every key, type and the row ordering it asserts. Adding a
    key is fine; renaming, removing or reordering one is a breaking change that
    test will catch here rather than in a browser at 2 a.m.
    """
    session = get_session()
    try:
        return regions_dashboard(session)
    finally:
        session.close()
