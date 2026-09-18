"""Placeholder for the memory tables (Phase 4). Filled in by the lane that owns this file.

Define rows against the shared ``Base`` from ``noc_agents.db.models``; this module is
imported by ``db/models_all.py`` before the migration runs, so anything declared here is
created by the generic additive path with no hand-written DDL.
"""

from __future__ import annotations

from noc_agents.db.models import Base  # noqa: F401  (re-exported for the lane's rows)
