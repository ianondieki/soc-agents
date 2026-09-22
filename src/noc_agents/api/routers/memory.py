"""Agent-memory read routes (spec §7.11, Phase 4 Lane 4C, step M0).

One route today: ``GET /api/v1/memory/sites/{site_id}`` — "what has happened at this site
before", which the incident workspace renders as the "Earlier at this site" panel. It is a
**read** surface over ``incidents`` + ``work_notes`` and nothing else; M0 adds no table, no
job and no write path (§7.11.10, the M0 row).

Three properties this file exists to hold:

* **Operator scoping happens in the query, not here.** ``services.memory`` builds every
  statement from ``api.deps._owned``; this module must never add a second, hand-rolled path
  to the same rows. See the module docstring there for why recall is the surface where a
  missing ``WHERE`` hurts most.
* **``MEMORY_ENABLED`` is enforced at this boundary.** §7.11.3 makes it an environment
  variable defaulting to **false**, and §7.11.11 test 25 requires that with the flag off
  every memory endpoint returns an empty result. It is checked here rather than inside the
  ``recall_*`` functions because those are also M1's consolidator inputs — see
  ``services/memory.py`` "WHERE THE FLAG IS CHECKED".
* **An unknown site is 200 with nothing in it, never 404 and never 500** (§7.11.4 route
  table). A site id is not a row id: "no history here" and "no such site" are the same
  answer to the question the panel is asking, and a 404 would make the workspace render an
  error where an empty panel is the truthful result.

The response is an **object**, not a bare list, because §7.11.4 specifies this route as
"``recall_site_history()`` rows plus the site's current facts": ``facts`` is the L2 half and
lands in M3, so it ships now as an always-empty list rather than as a key that appears later
and breaks a renderer written against its absence. ``enabled`` and ``degraded`` are on the
wire for the same reason the Wallboard shows ``STALE`` badges (§7.10, the 3 a.m. rules): a
panel that is empty because the feature is off must not look like a site with a clean record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from noc_agents.api.auth import require_role
from noc_agents.api.deps import MEMORY_READERS
from noc_agents.db.models import get_session
from noc_agents.services.clock import z_utc
from noc_agents.services.memory import episode_dicts, memory_enabled, recall_site_history

router = APIRouter(prefix="/api/v1", tags=["memory"])

#: Defaults for the site-history window, matching ``recall_site_history``'s own defaults
#: (§7.11.4). ``site_lookback_days: 365`` is the §7.11.3 YAML value; it moves into the
#: operator profile in M1, when ``OperatorConfig`` gains the ``memory`` field.
DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_LIMIT = 20
#: Upper bounds are enforced by FastAPI so a hand-typed query string is a 422, not a 500 and
#: not a full-table scan rendered into a panel.
MAX_LOOKBACK_DAYS = 3650
MAX_LIMIT = 200


def _z(row: dict[str, Any]) -> dict[str, Any]:
    """Stamp outgoing timestamps with an explicit ``Z`` (spec §7.0.6, defect #41).

    The same rule ``api/serializers.py`` applies, applied by ``isinstance`` for the same
    reason: a naive ISO string is read by a browser as *local* time, which in Nairobi
    backdates every episode by three hours — and "the last outage here was 3 h earlier than
    it was" is exactly the kind of quiet wrongness a memory panel must not introduce.
    """
    return {k: (z_utc(v) if isinstance(v, datetime) else v) for k, v in row.items()}


# §9.3's memory row ("sites ... read"): legal R, msp_coordinator and field_engineer "—".
# §7.11.4's "any signed-in role" conflicts with it; api/deps.MEMORY_READERS says why §9.3
# wins, and main.get_incident withholds the advisory built from these same episodes.
@router.get("/memory/sites/{site_id}", dependencies=[Depends(require_role(*MEMORY_READERS))])
def get_site_memory(
    site_id: str,
    lookback_days: int = Query(DEFAULT_LOOKBACK_DAYS, ge=0, le=MAX_LOOKBACK_DAYS),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """Prior finished incidents at one site, newest first, plus (from M3) the site's facts.

    Advisory and inert: this route reads, and reading it changes no incident, no decision
    field, no step row and no event (MEM1). It is safe to call on every workspace load.
    """
    enabled = memory_enabled()
    episodes: list[dict[str, Any]] = []
    if enabled:
        session = get_session()
        try:
            episodes = [
                _z(row)
                for row in episode_dicts(
                    recall_site_history(session, site_id=site_id, lookback_days=lookback_days, limit=limit)
                )
            ]
        finally:
            session.close()
    return {
        "site_id": site_id,
        "enabled": enabled,
        "lookback_days": lookback_days,
        "episodes": episodes,
        # L2 entity facts (dominant failure domain, seasonal peak, median restore) are M3;
        # the key is present and empty so a renderer written today keeps working then.
        "facts": [],
        # The flag is off, so an empty ``episodes`` says nothing about the site. Without this
        # the panel would read as "this site has a clean record" on every deployment that
        # never opted in. It is NOT a recall-error signal: a degraded recall (MEM4) returns
        # ``()`` indistinguishably from a site with no history, and M1's
        # ``GET /memory/stats`` (the degraded-recall counter) is where that becomes visible.
        "degraded": not enabled,
    }
