"""``render_statuspage(alert)`` (§6.2, CONFORMANCE C-08): ``{status, impact}`` in Statuspage's words.

Pinned: the exact key set §6.2 names; the four lifecycle words map one-to-one to Statuspage's
incident statuses, from the incident status through ``build_alert``'s own ``lifecycle_for``;
impact follows SEVERITY's priority (P1 critical … P4 none) and nothing a model drafted; the
output is deterministic; an envelope outside either vocabulary raises instead of guessing.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from noc_agents.config import get_settings
from noc_agents.db.models import IncidentRow
from noc_agents.domain.alerts import Content
from noc_agents.services import render
from noc_agents.services.alerts import build_alert
from noc_agents.services.render.statuspage import (
    STATUSPAGE_IMPACT_BY_PRIORITY,
    STATUSPAGE_STATUSES,
    render_statuspage,
)

SENT = datetime(2026, 9, 16, 10, 47, 10)


@pytest.fixture()
def cfg():
    return get_settings().operator


def _row(**overrides) -> IncidentRow:
    base = dict(
        id="inc-0001",
        operator_id="safaricom",
        incident_number="INC000123",
        status="ASSIGNED",
        priority="P2",
        users_affected=450000,
        site_id="SFC-NBIE-HUB-EMB",
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        region_code="NBI_E",
        title="HUB POWER — Embakasi East",
        narrative="Service-affecting event.",
        assignee_type="MSP",
        assignee_name="EGYPRO",
        msp_name="EGYPRO",
        correlation_fingerprint="SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER",
        failure_domain="POWER",
        tt_category="POWER_GRID",
        child_site_ids_json="[]",
        radio_oem="MIXED",
        msp_root_cause=None,
    )
    base.update(overrides)
    return IncidentRow(**base)


def _alert(cfg, **row):
    return build_alert(_row(**row), cfg, sent=SENT, alert_id="alert-1")


def test_the_dict_is_exactly_the_two_keys_section_6_2_names(cfg):
    assert render_statuspage(_alert(cfg)) == {"status": "investigating", "impact": "major"}


@pytest.mark.parametrize(
    "row,status",
    [
        (dict(status="TICKETED"), "investigating"),
        (dict(status="ASSIGNED"), "investigating"),
        (dict(status="AWAITING_VENDOR"), "investigating"),
        (dict(status="IN_PROGRESS"), "investigating"),  # no root cause named yet
        (dict(status="IN_PROGRESS", msp_root_cause="Rectifier failed"), "identified"),
        (dict(status="RESTORED"), "monitoring"),
        (dict(status="CLOSED"), "resolved"),
    ],
)
def test_status_follows_the_envelope_lifecycle(cfg, row, status):
    alert = _alert(cfg, **row)
    assert render_statuspage(alert)["status"] == status == alert.classification.lifecycle.lower()


@pytest.mark.parametrize("priority,impact", [("P1", "critical"), ("P2", "major"), ("P3", "minor"), ("P4", "none")])
def test_impact_follows_the_priority(cfg, priority, impact):
    assert render_statuspage(_alert(cfg, priority=priority))["impact"] == impact


def test_a_resolved_incident_keeps_its_impact(cfg):
    assert render_statuspage(_alert(cfg, priority="P1", status="CLOSED")) == {"status": "resolved", "impact": "critical"}


def test_the_vocabularies_are_statuspages_own():
    assert STATUSPAGE_STATUSES == ("investigating", "identified", "monitoring", "resolved")
    assert set(STATUSPAGE_IMPACT_BY_PRIORITY.values()) == {"none", "minor", "major", "critical"}


def test_deterministic_and_blind_to_drafted_content(cfg):
    """Same envelope, same dict; and AI content cannot move either word."""
    alert = _alert(cfg)
    drafted = alert.model_copy(update={"content": {"en": Content(headline="All fine", body="Nothing to see.")}})
    assert render_statuspage(alert) == render_statuspage(alert) == render_statuspage(drafted)


def test_an_unmapped_envelope_raises_rather_than_inventing_a_word(cfg):
    alert = _alert(cfg)
    odd = alert.model_copy(update={"classification": alert.classification.model_copy(update={"lifecycle": "PAUSED"})})
    with pytest.raises(ValueError, match="no Statuspage status"):
        render_statuspage(odd)
    odd = alert.model_copy(update={"classification": alert.classification.model_copy(update={"priority": "P5"})})
    with pytest.raises(ValueError, match="no Statuspage impact"):
        render_statuspage(odd)


def test_it_is_exported_beside_its_siblings():
    assert render.render_statuspage is render_statuspage and "render_statuspage" in render.__all__
