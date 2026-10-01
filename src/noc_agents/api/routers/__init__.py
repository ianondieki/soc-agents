"""API router modules, one per Phase 4 feature lane.

``main.py`` does ``for r in ROUTERS: app.include_router(r)`` once, so a lane owns exactly
one file here and never edits ``main.py``. Shared helpers (RBAC allow-lists, ``_owned``,
``_actor``) come from ``noc_agents.api.deps``; importing ``noc_agents.main`` from a router
is a cycle and will fail at startup.

Every route a lane adds is still subject to the same two rules as the routes in ``main.py``:
read operator-owned tables through ``_owned``/``_get_owned``, and gate writes with
``Depends(require_role(...))``.

The list is explicit rather than auto-discovered: a module that fails to import should break
the app loudly at startup, not silently drop its routes.
"""

from __future__ import annotations

from fastapi import APIRouter

from noc_agents.api.routers import (
    admin,
    capacity,
    clocks,
    complaints,
    contracts,
    dashboards,
    maintenance,
    memory,
    outbox_admin,
    pir,
    productivity,
    regulatory,
    scorecards,
    signals,
    templates,
    vendors,
)

ROUTERS: tuple[APIRouter, ...] = (
    vendors.router,
    clocks.router,
    scorecards.router,
    regulatory.router,
    pir.router,
    dashboards.router,
    memory.router,
    # Phase 5 lanes
    maintenance.router,
    capacity.router,
    contracts.router,
    complaints.router,
    # Conformance batch (docs/CONFORMANCE.md C-09, C-10, C-11, C-15)
    templates.router,
    outbox_admin.router,
    signals.router,
    admin.router,
    # Showcase: agent throughput and the toil-minutes model
    productivity.router,
)

__all__ = ["ROUTERS"]
