"""Agent memory — the **write** side of §7.11 (Phase 4 Lane 4C, step M1).

Two modules, both deliberately small:

* :mod:`noc_agents.memory.schema` — ``ensure_memory_schema()``, the FTS5 virtual table. A
  virtual table cannot be a ``Base`` subclass, so it cannot live in ``Base.metadata`` and is
  created lazily with ``IF NOT EXISTS``, exactly as ``services/contracts.ensure_fts`` creates
  ``contract_clauses_fts``.
* :mod:`noc_agents.memory.consolidate` — ``consolidate_incident()``, the ``memory_consolidate``
  job and ``expire_memory()``, the seam ``services/housekeeping.py`` already names
  (``MEMORY_EXPIRY_SEAM = "noc_agents.memory.consolidate:expire_memory"``).

**Where the read side lives.** §7.11.4 sketches ``memory/recall.py`` and ``memory/render.py``
beside these. M0 shipped every read API in ``services/memory.py`` and its three test files are
written against that import path; moving them here would be a rename with no behaviour in it,
and the FTS tier, ``recall_for_incident()`` and ``advisory_block()`` therefore extend that
module instead. They move here when M3 adds ``memory_facts`` and there is a second table's
worth of read code to justify the package. The split that matters — and the one this package
exists to hold — is **write side here, read side there**, because MEM5's rule is about *who
may write*: nothing in this package may be imported by the hot path.

Nothing here is imported by ``orchestrator/runner.py``, ``graph/pipeline.py`` or any
deterministic engine (MEM1/G15, pinned by an AST walk in
``tests/unit/test_memory_advisory_is_inert.py``). Writers run **post-commit only**: the
scheduled job (a fresh session per tick), ``scripts/backfill_memory.py`` and
``HousekeepingAgent``. A fail-closed incident run rolls back its own transaction and has
nothing of ours in it to take down (MEM5).
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
