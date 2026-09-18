"""Out-of-band pollers (spec §4.4, §7.3): scheduler jobs that pull the outside world into
the ``external_signals`` cache so nothing on the incident hot path ever touches the network.

Each poller module exposes a ``poll(session, settings) -> JobResult`` and a module-level
``JobCard`` the scheduler registers. Every one is *fail-soft* by contract: a provider that
times out, answers 500, returns garbage or fails TLS is logged and recorded on the row, and
the last good row stays in place, labelled stale once its ``valid_until`` passes. A poller
never raises into the scheduler loop.

Modules: :mod:`noc_agents.pollers.weather` (``weather_regions``, Phase 3). The KMD CAP, GloFAS
flood, KPLC and complaint pollers are later Phase 3/6 waves and will live beside it.
"""

from __future__ import annotations

__all__: list[str] = []
