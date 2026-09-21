"""One-off backfill of the agent-memory episode index (spec §7.11.5, Lane 4C step M1).

``memory_episodes`` and ``memory_note_fts`` are **derived**: every row in them is arithmetic
over ``incidents`` and ``work_notes``. The ``memory_consolidate`` job only ever looks at what
finished since its last tick, so a database that already has history — which is every real
one — needs one pass over the back catalogue before recall has anything to find. That is this
script, and it is the same code the job runs, one incident at a time.

Three properties, each of which is why this is a script and not a migration:

* **Idempotent.** ``consolidate_incident`` updates the row keyed on a UNIQUE ``incident_id``,
  so running this twice produces the same index, not a second copy.
* **Resumable.** Progress is recorded in the rows themselves (``built_at`` /
  ``source_version``), not in a cursor file, so an interrupted run is continued by simply
  running it again. It commits every 50 incidents (§4.5's short-transaction rule), so an
  interruption loses at most the batch in flight — and a batch costs seconds.
* **Safe to re-run after a ``SOURCE_VERSION`` bump.** ``--rebuild`` re-derives every episode,
  which is what a change to the derivation needs and what no schema migration can do.

It writes nothing outside ``memory_*``: no incident, note, run, audit-of-anything-else or
outbox row is touched. It makes no network call and needs no LLM (§7.11.9: the memory layer's
own cost is zero tokens). It honours ``MEMORY_ENABLED`` only in the sense that it prints a
warning when the flag is off — building the index is what makes the flag worth turning on, so
refusing to build it while it is off would be a catch-22.

Usage (Windows, from the repo root)::

    C:\\Python313\\python.exe scripts/backfill_memory.py --dry-run
    C:\\Python313\\python.exe scripts/backfill_memory.py
    C:\\Python313\\python.exe scripts/backfill_memory.py --since 2025-01-01 --limit 5000
    C:\\Python313\\python.exe scripts/backfill_memory.py --rebuild        # after a SOURCE_VERSION bump
    C:\\Python313\\python.exe scripts/backfill_memory.py --operator airtel

``--operator`` sets ``OPERATOR_PROFILE`` before the settings are read, because the operator
clause comes from ``api.deps._owned`` and therefore from the **active profile**: both
operators share one SQLite file (``config/default.yaml``), so backfilling both means running
this twice, once per profile. Doing it in one pass would mean this script building its own
operator clause, and there is exactly one place that is allowed to do that.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # runnable without an editable install
    sys.path.insert(0, str(ROOT / "src"))


def _parse_since(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` (or a full ISO timestamp) as naive UTC — the storage contract."""
    if not value:
        return None
    text = value.strip().replace("Z", "")
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill memory_episodes + memory_note_fts")
    parser.add_argument("--since", help="only incidents created on or after this date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many incidents")
    parser.add_argument("--batch", type=int, default=50, help="incidents per commit (default 50)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="re-derive every episode, including ones already current (use after a SOURCE_VERSION bump)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be built and write nothing",
    )
    parser.add_argument("--operator", help="OPERATOR_PROFILE to back-fill (default: the current one)")
    args = parser.parse_args(argv)

    if args.operator:
        # Set BEFORE the first settings read: the operator clause comes from the active
        # profile, so this must win over anything already cached.
        os.environ["OPERATOR_PROFILE"] = args.operator

    from noc_agents.config import clear_settings_cache, get_settings
    from noc_agents.db.models import get_session, init_db
    from noc_agents.memory.consolidate import (
        SOURCE_VERSION,
        backfill_plan,
        consolidate_all,
        episode_counts,
    )
    from noc_agents.db.models import utcnow
    from noc_agents.services.memory import MEMORY_ENABLED_ENV, memory_enabled, memory_settings

    clear_settings_cache()
    settings = get_settings()
    init_db(settings.database_url)
    session = get_session()
    try:
        operator = settings.operator.operator_id
        since = _parse_since(args.since)
        print(f"operator={operator} db={settings.database_url} source_version={SOURCE_VERSION}")
        print(f"before: {episode_counts(session)}")
        if not memory_enabled():
            # A warning, not a refusal: the index has to exist before the flag is worth
            # turning on, and consolidation writes nothing anybody can read until it is.
            # ASCII only in what this prints: a Windows console on a legacy code page raises
            # UnicodeEncodeError on an em dash, and a backfill must not die on its own banner.
            print(f"note: {MEMORY_ENABLED_ENV} is off -- the index will be built but no route will serve it")

        if args.dry_run:
            # Deliberately a *count*, made with the same "not current" predicate the real run
            # uses — not a simulated write. Anything more would be a second implementation of
            # the derivation, and a dry run that does not exercise the real path only tells
            # you about the dry run.
            # Bounded by the same 24-month line the backfill itself honours (review M12).
            horizon = utcnow() - timedelta(
                days=int(memory_settings(settings.operator)["episode_max_age_days"])
            )
            plan = backfill_plan(session, since=since, horizon=horizon)
            if args.rebuild:
                plan["to_build"] = plan["finished"]
            print(f"dry-run: {plan}; nothing written")
            return 0

        def progress(done: int, written: int) -> None:
            print(f"  ... {done} considered, {written} episodes written")

        counts = consolidate_all(
            session,
            settings=settings,
            since=since,
            batch=max(1, args.batch),
            limit=args.limit,
            rebuild=args.rebuild,
            on_batch=progress,
        )
        session.commit()
        print(f"done: {counts}")
        print(f"after:  {episode_counts(session)}")
        return 0
    finally:
        session.close()
        clear_settings_cache()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
