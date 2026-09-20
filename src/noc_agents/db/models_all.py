"""Import every model module, so ``Base.metadata`` is complete before the migration runs.

``init_db`` imports this one module. A table declared in a module nobody imports is
invisible to ``migrate_additive`` (which walks ``Base.metadata.sorted_tables``) and to
``create_all`` -- it would simply never be created, and the first query against it would
fail at runtime rather than at startup. Adding a model module means adding it here.
"""

from __future__ import annotations

from noc_agents.db import (  # noqa: F401  (imported for the side effect of registering tables)
    models_capacity,
    models_complaints,
    models_contracts,
    models_maintenance,
    models_memory,
    models_pir,
    models_regulatory,
    models_scorecards,
    models_vendors,
)
