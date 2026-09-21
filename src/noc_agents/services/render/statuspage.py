"""Statuspage renderer (§6.2): the two words a public status page shows for one alert.

``render_statuspage(alert) -> {"status": ..., "impact": ...}`` — exactly the dict §6.2 names,
in Atlassian Statuspage's own vocabulary (https://developer.statuspage.io/: incident ``status``
investigating / identified / monitoring / resolved; ``impact`` none / minor / major / critical),
which §6 borrows for the envelope's external lifecycle words.

* ``status`` is ``classification.lifecycle`` lower-cased. The envelope's ``Lifecycle`` literal
  IS the Statuspage status set, so this is a spelling change, not a mapping anyone can drift.
* ``impact`` comes from ``classification.priority`` — SEVERITY's decision, never a model's —
  one step per priority: P1 critical, P2 major, P3 minor, P4 none. It stays the incident's
  impact after it resolves (Statuspage keeps an incident's impact on the resolved record).

Nothing else is rendered on purpose. ``STATUSPAGE`` is a ``Channel`` literal and nothing more:
no adapter, no page id, no config that lists the channel, and the audiences a public page would
serve (CUSTOMER / PUBLIC) are HITL-gated by §6.1 and out of scope under D10 — so free text for a
public page would be text nobody has approved. Like ``render_ledger_row`` it is one dict per
alert, not a per-audience ``ChannelPayload``. Pure: no session, no clock, no config, and an
alert outside the two vocabularies raises rather than inventing a word.
"""

from __future__ import annotations

from noc_agents.domain.alerts import NocAlert

__all__ = ["STATUSPAGE_IMPACT_BY_PRIORITY", "STATUSPAGE_STATUSES", "render_statuspage"]

#: Statuspage incident statuses, in lifecycle order — the envelope's ``Lifecycle`` lower-cased.
STATUSPAGE_STATUSES: tuple[str, ...] = ("investigating", "identified", "monitoring", "resolved")

#: SEVERITY's priority → Statuspage incident impact.
STATUSPAGE_IMPACT_BY_PRIORITY: dict[str, str] = {
    "P1": "critical",
    "P2": "major",
    "P3": "minor",
    "P4": "none",
}


def render_statuspage(alert: NocAlert) -> dict:
    """``{"status": investigating|identified|monitoring|resolved, "impact": none|minor|major|critical}``."""
    status = alert.classification.lifecycle.lower()
    if status not in STATUSPAGE_STATUSES:
        raise ValueError(f"lifecycle {alert.classification.lifecycle!r} has no Statuspage status")
    impact = STATUSPAGE_IMPACT_BY_PRIORITY.get(alert.classification.priority)
    if impact is None:
        raise ValueError(f"priority {alert.classification.priority!r} has no Statuspage impact")
    return {"status": status, "impact": impact}
