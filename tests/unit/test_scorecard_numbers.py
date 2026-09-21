"""Spec §7.6.2 / §7.6.8 -- the vendor scorecard's NUMBERS, worked by hand (Phase 4 Lane 4A).

This is the test a vendor's commercial team will eventually be shown, so the fixture is
small enough to check with a pencil and every expected figure below is derived in this
docstring, not copied from the code's output. If the code and this arithmetic ever
disagree, find out which is wrong before changing either.

THE FIXTURE
===========
Vendor EGYPRO (operator safaricom), period **2026-08**, terms ``config/sla_terms.yaml``
version 2026-09: P1 ack 5 / restore 60 / note 15; P2 10 / 120 / 30; P3 20 / 240 / 60;
P4 30 / 480 / 120. Region multipliers: NBI_E 1.0, MTK 1.15, CST 1.25.

The period is the EAT calendar month: [2026-07-31 21:00Z, 2026-08-31 21:00Z), 31 days,
44 640 minutes. All times below are UTC, day/hh:mm, August 2026 unless stated.

    #    P   site  region  domain  failure      escalated  1st note  restored      source      stop clocks
    G00  P3  S-Z   NBI_E   POWER   Jul31 20:30  20:35      20:50     Jul31 22:30   MARK        --            (July's incident)
    G01  P1  S-A   NBI_E   POWER   03/06:00     06:02      06:06     08:00         MARK        UTILITY_POWER 06:10-06:40 + SITE_ACCESS_DENIED 06:30-07:00 (OVERLAP)
    G02  P1  S-B   NBI_E   POWER   05/10:00     10:01      10:04     10:45         SUPERVISOR  OBSERVATION 10:05-10:35 REVERSED (recorded 75 min late)
    G03  P2  S-C   MTK     TX      07/12:00     12:05      12:17     14:18         MARK        --
    G04  P2  S-A   NBI_E   POWER   10/09:00     09:03      09:11     10:40         MARK        UTILITY_POWER 09:20-09:50 (recorded 90 min late)   <- REPEAT of S-A|POWER
    G05  P2  S-D   NBI_E   POWER   12/20:00     20:04      20:14     21:30         INFERRED    --
    G06  P3  S-E   CST     POWER   15/02:00     02:10      02:40     05:00         SUPERVISOR  --
    G07  P3  S-F   NBI_E   ENV     18/08:00     08:05      (none)    13:00         SUPERVISOR  --
    G08  P3  S-F   NBI_E   POWER   20/08:00     08:10      08:25     10:00         MARK        --            (NOT service-affecting)
    G09  P4  S-G   NBI_E   POWER   21/10:00     10:05      --        --            --          --            CANCELLED
    G10  P4  S-H   NBI_E   POWER   21/23:00     23:05      23:15     22/03:00      MARK        --            planned_maintenance = 1
    G11  P4  S-I   NBI_E   POWER   22/06:00     06:20      06:45     12:00         MARK        PLANNED_MAINTENANCE 09:00-11:00; a SCHEDULED window covers S-I 08:00-10:00
    G12  P2  S-J   NBI_E   POWER   31/15:00     15:10      15:30     (still down)  --          SITE_ACCESS_DENIED 18:00-(STILL OPEN)
    G13  P4  S-K   NBI_E   TX      25/04:00     04:30      05:10     13:30         MARK        AWAITING_THIRD_PARTY_PERMIT 06:00-07:00

Noise that must not leak in: an EGYPRO incident failing at exactly 31/21:00Z (that is 00:00
EAT on 1 September -- next month's), a TETRANET incident in August, and an AIRTEL incident
carrying EGYPRO's vendor_id.

Eligible (vendor assigned, not CANCELLED, not planned): all but G09, G10. G00 belongs to July
for every KPI except availability, where its 21:00-22:30 tail is inside August.

ADJUSTED RESTORE = restored - failure - UNION of un-reversed stop clocks
    G01  120 - 50 = 70    the two clocks cover 06:10-07:00 = 50 min. Their SUM is 60, and
                          120 - 60 = 60 would PASS the 60-minute P1 limit: double-deducting
                          an overlap turns this breach into a pass. The union does not.
    G02   45 -  0 = 45    the 30-minute clock was reversed: it deducts nothing
    G03  138              G04  100 - 30 = 70        G06  180        G07  300        G08  120
    G11  360 - 120 = 240  G13  570 - 60 = 510
    G05  not measured: its restore time was inferred from a note.
    G12  no restore. At the period end (31/21:00) it has been down 360 min, the open stop
         clock is bounded there (18:00-21:00 = 180), adjusted elapsed = 180 > 120: a breach
         already, whatever happens next.

MTTA_MIN = median(first vendor note - escalated)
    P1  [3, 4]                                  -> 3.5
    P2  [8, 10, 12, 20]   (G04 G05 G03 G12)     -> 11     normalised [8, 10, 12/1.15 = 10.4348, 20] -> (10 + 10.4348)/2 = 10.2174 -> 10.22
    P3  [15, 30]          (G08 G06; G07 none)   -> 22.5   normalised [15, 30/1.25 = 24] -> 19.5
    P4  [25, 40]          (G11 G13)             -> 32.5
    ALL [3,4,8,10,12,15,20,25,30,40]            -> 13.5   normalised [...,10, 10.4348, 15,...] -> (10.4348 + 15)/2 = 12.7174 -> 12.72

ADJ_MTTR_MIN = median(adjusted restore) over trusted restores
    P1  [45, 70] -> 57.5   (50 stop-clock min)
    P2  [70, 138] -> 104   (30)   normalised [70, 138/1.15 = 120] -> 95      excluded: G05 inferred, G12 not restored
    P3  [120, 180, 300] -> 180    normalised [120, 180/1.25 = 144, 300] -> 144
    P4  [240, 510] -> 375  (120 + 60 = 180)
    ALL [45,70,70,120,138,180,240,300,510] -> 138  (260)   normalised [45,70,70,120,120,144,240,300,510] -> 120

SLA_COMPLIANCE_PCT = 100 x (adjusted restore <= limit) / eligible
    P1  G01 70 > 60 no, G02 yes                 -> 1/2  = 50       RED
    P2  G03 138 > 120 no, G04 yes, G12 no       -> 1/3  = 33.33    RED   normalised: G03 120 <= 120 yes -> 2/3 = 66.67
    P3  G06 yes, G07 300 > 240 no, G08 yes      -> 2/3  = 66.67    RED
    P4  G11 yes, G13 510 > 480 no               -> 1/2  = 50       RED
    ALL 5/10 = 50  RED   normalised 6/10 = 60   stop-clock min 50 + 30 + 180 (G12) + 180 = 440
    Credit: EGYPRO is escalating_consecutive [15, 30, 50]; first RED period -> 15 %, PROPOSED.

REPEAT_FAULT_RATE = sites with >= 2 incidents of one site|domain signature / affected sites
    affected S-A S-B S-C S-D S-E S-F S-I S-J S-K = 9; S-A has POWER twice. S-F has two
    incidents but different domains, so it is not a repeat.                  -> 1/9 = 0.1111

NOTE_COMPLIANCE_PCT = 100 x met slots / expected slots; slot = note_interval x multiplier,
counted from escalated to restored (period end for G12); expected = COMPLETE slots
    G01  118 min / 15 -> 7 slots; notes at +4 +18 +33 +48 +78 +98 (+116 is past slot 7) hit 0 1 2 3 5 6  -> 6/7
    G02   44 / 15 -> 2; +3 +19                                                                          -> 2/2
    G03  133 / 34.5 -> 3; +12 +66 +100 hit (0,34.5] (34.5,69] (69,103.5]                                -> 3/3
         (at the unmultiplied 30 min it would be 4 slots and (30,60] is empty: 3/4. The allowance matters.)
    G04   97 / 30 -> 3; +8 +42 +75 -> 3/3        G05  86 / 30 -> 2; +10 +45 (+86 is past) -> 2/2
    G06  170 / 75 -> 2; +30 +110 -> 2/2          G07  295 / 60 -> 4; no VENDOR note (NOC chasers do not count) -> 0/4
    G08  110 / 60 -> 1; +15 -> 1/1               G11  340 / 120 -> 2; +25 +160 -> 2/2
    G12  350 / 30 -> 11; +20 +50 +90 +115 +145 +175 +200 +240 hit slots 0-7 -> 8/11
    G13  540 / 120 -> 4; +40 +150 +290 +390 -> 4/4
    P1 8/9 = 88.89 AMBER   P2 16/19 = 84.21 AMBER   P3 3/7 = 42.86 RED   P4 6/6 = 100 GREEN
    ALL 33/41 = 80.49 AMBER

AVAILABILITY_PCT = 100 x (uptime - unavailable) / uptime, uptime = 44 640 x sites in scope
    unavailable per site (outage clipped to August, minus stop clocks, minus planned window):
    S-Z 90 (G00's tail)   S-A 70 + 70 = 140   S-B 45   S-C 138   S-D 90   S-E 180
    S-F 300 (G08 kept the site on air)        S-J 360 - 180 = 180         S-K 570 - 60 = 510
    S-I 360 - 120 (stop clock 09-11) - 60 = 180: the window is 08:00-10:00 but its second
        hour is ALREADY inside the stop clock, so only 08:00-09:00 is excluded again.
    total 1853 min over 10 affected sites: 100 x (446 400 - 1853) / 446 400 = 99.5849
    With the maintenance lane OFF the window is not looked at: 1913 min -> 99.5715.
    No contracted site list exists, so the band is NA. With sites_in_scope: 40 it would be
    100 x (1 785 600 - 1853) / 1 785 600 = 99.8962, GREEN.

DATA QUALITY: 11 eligible, 10 restored, 1 inferred = 10.0 %, not ABOVE 10 -> the gate passes.
DISCIPLINE: 2 stop clocks recorded more than 60 min late (G02's, although reversed; G04's).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction

import pytest
import yaml
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, BroadcastRow, IncidentRow, OutboxRow, WorkNoteRow
from noc_agents.db.models_maintenance import WINDOW_PROPOSED, WINDOW_SCHEDULED, MaintenanceWindowRow
from noc_agents.db.models_scorecards import (
    BAND_AMBER,
    BAND_GREEN,
    BAND_NA,
    BAND_RED,
    CREDIT_NONE,
    CREDIT_PROPOSED,
    KPI_ADJ_MTTR,
    KPI_AVAILABILITY,
    KPI_MTTA,
    KPI_NOTE_COMPLIANCE,
    KPI_REPEAT_FAULT,
    KPI_SLA_COMPLIANCE,
    KPIS,
    STATUS_SHADOW,
    VendorScorecardRow,
)
from noc_agents.db.models_vendors import ClockEventRow
from noc_agents.services import scorecard as sc
from noc_agents.services.clock_events import deducted_minutes
from noc_agents.services.vendors import DEFAULT_SLA_TERMS_PATH, load_sla_terms, vendor_id_for, VENDOR_SEED_ACTIVE_FROM

PERIOD = "2026-08"
NOW = datetime(2026, 9, 21, 6, 0)  # any instant after the period has ended
ALL = None


def _t(day: int, hh: int, mm: int = 0, *, month: int = 8) -> datetime:
    return datetime(2026, month, day, hh, mm)


# (number, priority, site, region, domain, failure, escalated, first_note, restored, source, extra)
INCIDENTS = [
    ("G00", "P3", "S-Z", "NBI_E", "POWER", _t(31, 20, 30, month=7), _t(31, 20, 35, month=7), _t(31, 20, 50, month=7), _t(31, 22, 30, month=7), "MARK_RESTORED", {}),
    ("G01", "P1", "S-A", "NBI_E", "POWER", _t(3, 6), _t(3, 6, 2), _t(3, 6, 6), _t(3, 8), "MARK_RESTORED", {}),
    ("G02", "P1", "S-B", "NBI_E", "POWER", _t(5, 10), _t(5, 10, 1), _t(5, 10, 4), _t(5, 10, 45), "SUPERVISOR", {}),
    ("G03", "P2", "S-C", "MTK", "TRANSMISSION", _t(7, 12), _t(7, 12, 5), _t(7, 12, 17), _t(7, 14, 18), "MARK_RESTORED", {}),
    ("G04", "P2", "S-A", "NBI_E", "POWER", _t(10, 9), _t(10, 9, 3), _t(10, 9, 11), _t(10, 10, 40), "MARK_RESTORED", {}),
    ("G05", "P2", "S-D", "NBI_E", "POWER", _t(12, 20), _t(12, 20, 4), _t(12, 20, 14), _t(12, 21, 30), "VENDOR_NOTE_INFERRED", {}),
    ("G06", "P3", "S-E", "CST", "POWER", _t(15, 2), _t(15, 2, 10), _t(15, 2, 40), _t(15, 5), "SUPERVISOR", {}),
    ("G07", "P3", "S-F", "NBI_E", "ENVIRONMENT", _t(18, 8), _t(18, 8, 5), None, _t(18, 13), "SUPERVISOR", {}),
    ("G08", "P3", "S-F", "NBI_E", "POWER", _t(20, 8), _t(20, 8, 10), _t(20, 8, 25), _t(20, 10), "MARK_RESTORED", {"service_affecting": False}),
    ("G09", "P4", "S-G", "NBI_E", "POWER", _t(21, 10), _t(21, 10, 5), None, None, None, {"status": "CANCELLED"}),
    ("G10", "P4", "S-H", "NBI_E", "POWER", _t(21, 23), _t(21, 23, 5), _t(21, 23, 15), _t(22, 3), "MARK_RESTORED", {"planned_maintenance": 1}),
    ("G11", "P4", "S-I", "NBI_E", "POWER", _t(22, 6), _t(22, 6, 20), _t(22, 6, 45), _t(22, 12), "MARK_RESTORED", {}),
    ("G12", "P2", "S-J", "NBI_E", "POWER", _t(31, 15), _t(31, 15, 10), _t(31, 15, 30), None, None, {"status": "IN_PROGRESS"}),
    ("G13", "P4", "S-K", "NBI_E", "TRANSMISSION", _t(25, 4), _t(25, 4, 30), _t(25, 5, 10), _t(25, 13, 30), "MARK_RESTORED", {}),
]

# vendor notes, as minutes after escalated_at (the offsets the docstring works with)
VENDOR_NOTES = {
    "G00": [15],
    "G01": [4, 18, 33, 48, 78, 98, 116],
    "G02": [3, 19],
    "G03": [12, 66, 100],
    "G04": [8, 42, 75],
    "G05": [10, 45, 86],
    "G06": [30, 110],
    "G07": [],
    "G08": [15],
    "G10": [10],
    "G11": [25, 160],
    "G12": [20, 50, 90, 115, 145, 175, 200, 240],
    "G13": [40, 150, 290, 390],
}

# (incident, code, start, end | None = still open, reversed, recorded N minutes after it started)
STOP_CLOCKS = [
    ("G01", "UTILITY_POWER", _t(3, 6, 10), _t(3, 6, 40), False, 5),
    ("G01", "SITE_ACCESS_DENIED", _t(3, 6, 30), _t(3, 7), False, 5),
    ("G02", "OBSERVATION", _t(5, 10, 5), _t(5, 10, 35), True, 75),
    ("G04", "UTILITY_POWER", _t(10, 9, 20), _t(10, 9, 50), False, 90),
    ("G11", "PLANNED_MAINTENANCE", _t(22, 9), _t(22, 11), False, 5),
    ("G12", "SITE_ACCESS_DENIED", _t(31, 18), None, False, 5),
    ("G13", "AWAITING_THIRD_PARTY_PERMIT", _t(25, 6), _t(25, 7), False, 5),
]

# (kpi, priority): (raw, normalised, region_multiplier_applied, band, eligible, excluded, scc_minutes)
GOLDEN = {
    (KPI_MTTA, "P1"): (3.5, 3.5, 1.0, BAND_NA, 2, 0, 0),
    (KPI_MTTA, "P2"): (11.0, 10.22, None, BAND_NA, 4, 0, 0),
    (KPI_MTTA, "P3"): (22.5, 19.5, None, BAND_NA, 2, 1, 0),
    (KPI_MTTA, "P4"): (32.5, 32.5, 1.0, BAND_NA, 2, 2, 0),
    (KPI_MTTA, ALL): (13.5, 12.72, None, BAND_NA, 10, 3, 0),
    (KPI_ADJ_MTTR, "P1"): (57.5, 57.5, 1.0, BAND_NA, 2, 0, 50),
    (KPI_ADJ_MTTR, "P2"): (104.0, 95.0, None, BAND_NA, 2, 2, 30),
    (KPI_ADJ_MTTR, "P3"): (180.0, 144.0, None, BAND_NA, 3, 0, 0),
    (KPI_ADJ_MTTR, "P4"): (375.0, 375.0, 1.0, BAND_NA, 2, 2, 180),
    (KPI_ADJ_MTTR, ALL): (138.0, 120.0, None, BAND_NA, 9, 4, 260),
    (KPI_SLA_COMPLIANCE, "P1"): (50.0, 50.0, 1.0, BAND_RED, 2, 0, 50),
    (KPI_SLA_COMPLIANCE, "P2"): (33.33, 66.67, None, BAND_RED, 3, 1, 210),
    (KPI_SLA_COMPLIANCE, "P3"): (66.67, 66.67, None, BAND_RED, 3, 0, 0),
    (KPI_SLA_COMPLIANCE, "P4"): (50.0, 50.0, 1.0, BAND_RED, 2, 2, 180),
    (KPI_SLA_COMPLIANCE, ALL): (50.0, 60.0, None, BAND_RED, 10, 3, 440),
    (KPI_REPEAT_FAULT, ALL): (0.1111, None, None, BAND_NA, 11, 2, 0),
    (KPI_NOTE_COMPLIANCE, "P1"): (88.89, None, 1.0, BAND_AMBER, 2, 0, 0),
    (KPI_NOTE_COMPLIANCE, "P2"): (84.21, None, None, BAND_AMBER, 4, 0, 0),
    (KPI_NOTE_COMPLIANCE, "P3"): (42.86, None, None, BAND_RED, 3, 0, 0),
    (KPI_NOTE_COMPLIANCE, "P4"): (100.0, None, 1.0, BAND_GREEN, 2, 2, 0),
    (KPI_NOTE_COMPLIANCE, ALL): (80.49, None, None, BAND_AMBER, 11, 2, 0),
    (KPI_AVAILABILITY, ALL): (99.5849, None, None, BAND_NA, 11, 3, 440),
}


def _incident(session, number, priority, site, region, domain, failure, escalated, first_note, restored, source, extra, *, operator_id="safaricom", msp="EGYPRO", vendor_id=None):
    inc = IncidentRow(
        operator_id=operator_id,
        incident_number=number,
        status=extra.get("status", "RESTORED" if restored else "IN_PROGRESS"),
        priority=priority,
        site_id=site,
        region_code=region,
        failure_domain=domain,
        correlation_fingerprint=f"{site}|X|{domain}",
        msp_name=msp,
        vendor_id=vendor_id,
        failure_time=failure,
        created_at=failure,
        escalated_at=escalated,
        first_vendor_note_at=first_note,
        restored_at=restored,
        restored_source=source,
        service_affecting=extra.get("service_affecting", True),
        planned_maintenance=extra.get("planned_maintenance", 0),
    )
    session.add(inc)
    session.flush()
    return inc


def build_fixture(session, *, late_openings: bool = True) -> dict[str, IncidentRow]:
    """The docstring's fixture, written to the database. ``late_openings=False`` records every
    stop clock promptly -- the same conditions, better operator paperwork."""
    rows: dict[str, IncidentRow] = {}
    for spec in INCIDENTS:
        rows[spec[0]] = _incident(session, *spec)
    for number, offsets in VENDOR_NOTES.items():
        inc = rows[number]
        for i, minutes in enumerate(offsets):
            session.add(
                WorkNoteRow(
                    incident_id=inc.id,
                    author="vendor engineer",
                    author_role="MSP" if i % 2 == 0 else "FE",
                    body="update",
                    source="ui",
                    created_at=inc.escalated_at + timedelta(minutes=minutes),
                )
            )
    # NOC chasers on the silent incident: they are OUR notes and must never count as the vendor's.
    for minutes in (30, 60, 90, 120, 180, 240):
        session.add(WorkNoteRow(incident_id=rows["G07"].id, author="WorklogMonitorAgent", author_role="AGENT", body="chase", source="monitor", created_at=rows["G07"].escalated_at + timedelta(minutes=minutes)))
        session.add(WorkNoteRow(incident_id=rows["G07"].id, author="J. Otieno", author_role="NOC", body="called vendor", source="ui", created_at=rows["G07"].escalated_at + timedelta(minutes=minutes + 5)))
    for number, code, start, end, reversed_, delay in STOP_CLOCKS:
        session.add(
            ClockEventRow(
                incident_id=rows[number].id,
                scc_code=code,
                started_at=start,
                ended_at=end,
                opened_by="J. Otieno",
                opened_role="noc_analyst",
                opened_at=start + timedelta(minutes=delay if late_openings else 2),
                reason="fixture",
                reversed_at=(end + timedelta(hours=1)) if reversed_ else None,
                reversed_by="A. Wanjiru" if reversed_ else None,
                reversal_reason="not a stop-clock condition" if reversed_ else None,
                created_at=start,
            )
        )
    # One SCHEDULED window on S-I (08:00-10:00), and a PROPOSED one that must count for nothing.
    for status, start, end in ((WINDOW_SCHEDULED, _t(22, 8), _t(22, 10)), (WINDOW_PROPOSED, _t(22, 6), _t(22, 12))):
        session.add(MaintenanceWindowRow(operator_id="safaricom", scope="SITE", scope_ref="S-I", starts_at=start, ends_at=end, uid=f"uid-{status}", organizer="noc", attendees_ref="audiences.FE_ONCALL", status=status))

    # --- noise that must not leak into EGYPRO's August card
    _incident(session, "N-SEPT", "P1", "S-A", "NBI_E", "POWER", _t(31, 21), _t(31, 21, 1), _t(31, 21, 2), _t(31, 23), "MARK_RESTORED", {})  # 00:00 EAT, 1 Sept
    _incident(session, "N-TETRA", "P1", "S-A", "NBI_E", "POWER", _t(9, 1), _t(9, 1, 1), _t(9, 1, 30), _t(9, 9), "MARK_RESTORED", {}, msp="TETRANET")
    egypro_id = vendor_id_for("safaricom", "EGYPRO", VENDOR_SEED_ACTIVE_FROM)
    _incident(session, "N-AIRTEL", "P1", "S-A", "NBI_E", "POWER", _t(9, 1), _t(9, 1, 1), _t(9, 1, 30), _t(9, 9), "MARK_RESTORED", {}, operator_id="airtel", vendor_id=egypro_id)
    session.flush()
    return rows


def _run(session, operator_id="safaricom") -> str:
    run = AgentRunRow(operator_id=operator_id, graph_name=sc.GRAPH_NAME, trigger="REQUEST", status="RUNNING")
    session.add(run)
    session.flush()
    return run.id


def _terms(cfg):
    return load_sla_terms(DEFAULT_SLA_TERMS_PATH, cfg=cfg)  # explicit path: a shell's SLA_TERMS_PATH cannot reach the golden test


def _card(session, code="EGYPRO", period=PERIOD) -> VendorScorecardRow:
    return session.scalars(select(VendorScorecardRow).where(VendorScorecardRow.period == period, VendorScorecardRow.vendor_id == vendor_id_for("safaricom", code, VENDOR_SEED_ACTIVE_FROM))).one()


def _numbers(session, card) -> dict:
    return {
        (l.kpi, l.priority): (l.raw_value, l.normalised_value, l.region_multiplier_applied, l.band, l.eligible_incidents, l.excluded_incidents, l.scc_minutes_deducted)
        for l in sc.lines_of(session, card.id)
    }


@pytest.fixture()
def golden(tmp_db, monkeypatch):
    """The fixture computed once, with the maintenance lane ON (so the planned window is excluded)."""
    settings, session = tmp_db
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    build_fixture(session)
    terms = _terms(settings.operator)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW)
    session.commit()
    return settings, session, terms


# --------------------------------------------------------------------------
# The golden numbers
# --------------------------------------------------------------------------


def test_the_golden_fixture_reproduces_every_hand_computed_number(golden):
    _settings, session, _terms_ = golden
    got = _numbers(session, _card(session))
    assert list(got) == list(sc.LINE_SHAPE)  # all 22 lines, in the fixed reading order
    for key, expected in GOLDEN.items():
        assert got[key] == expected, f"{key}: code says {got[key]}, the hand calculation says {expected}"
    assert set(got) == set(GOLDEN)


def test_the_golden_card_is_shadow_passes_the_gate_at_exactly_ten_percent_and_names_its_terms(golden):
    _settings, session, terms = golden
    card = _card(session)
    assert card.status == STATUS_SHADOW and card.shadow_required == 1  # EGYPRO's first period
    assert card.published_at is None and card.dispute_window_ends_at is None
    assert (card.period_start, card.period_end) == (datetime(2026, 7, 31, 21, 0), datetime(2026, 8, 31, 21, 0))
    dq = card.data_quality
    assert (dq["incidents"], dq["restored_incidents"], dq["inferred_restores"], dq["inferred_pct"]) == (11, 10, 1, 10.0)
    assert dq["passed"] is True and dq["gate_threshold_pct"] == 10.0 and dq["inferred_incidents"] == ["G05"]
    assert dq["inferred_by_source"] == {"VENDOR_NOTE_INFERRED": 1}
    # the CHECK's operands are the JSON's numbers, not a second opinion
    assert (card.dq_restored_incidents, card.dq_inferred_restores, card.dq_gate_threshold_pct) == (10, 1, 10.0)
    assert card.sla_terms_version == terms.version == "2026-09"
    assert session.get(AgentRunRow, card.computed_by_run_id) is not None
    # "defaults, not contract" -- on the card
    assert card.terms["basis"] == "DEFAULTS_NOT_CONTRACT" and card.terms["contract_is_synthetic"] is True
    assert card.terms["notice"].startswith("defaults, not contract")
    assert sc.scorecard_out(card)["terms_notice"].startswith("defaults, not contract")


def test_the_inferred_restore_and_the_open_incident_are_listed_as_excluded_by_name(golden):
    _settings, session, _terms_ = golden
    lines = {(l.kpi, l.priority): l for l in sc.lines_of(session, _card(session).id)}
    mttr = lines[(KPI_ADJ_MTTR, "P2")].evidence
    assert mttr["excluded"] == [
        {"incident": "G05", "reason": "UNTRUSTED_RESTORE_SOURCE:VENDOR_NOTE_INFERRED"},
        {"incident": "G12", "reason": "NOT_RESTORED"},
    ]
    sla = lines[(KPI_SLA_COMPLIANCE, "P2")].evidence
    assert sla["excluded"] == [{"incident": "G05", "reason": "UNTRUSTED_RESTORE_SOURCE:VENDOR_NOTE_INFERRED"}]
    g12 = next(row for row in sla["measured"] if row["incident"] == "G12")
    assert g12 == {"incident": "G12", "kind": "OPEN_BREACHED", "adjusted_minutes": 180.0, "limit_minutes": 120, "compliant": False, "compliant_normalised": False, "in_normalised_denominator": True, "scc_minutes": 180}
    assert lines[(KPI_MTTA, "P3")].evidence["excluded"] == [{"incident": "G07", "reason": "NO_VENDOR_NOTE"}]
    assert {row["reason"] for row in lines[(KPI_MTTA, "P4")].evidence["excluded"]} == {"CANCELLED", "PLANNED_MAINTENANCE"}
    avail = lines[(KPI_AVAILABILITY, ALL)].evidence
    assert {row["incident"]: row["reason"] for row in avail["excluded"]} == {"G08": "NOT_SERVICE_AFFECTING", "G09": "CANCELLED", "G10": "PLANNED_MAINTENANCE"}
    by_site = {row["site_id"]: row for row in avail["sites"]}
    assert by_site["S-I"] == {"site_id": "S-I", "incidents": ["G11"], "gross_minutes": 360.0, "scc_minutes": 120, "planned_minutes": 60, "unavailable_minutes": 180.0}
    assert by_site["S-Z"]["unavailable_minutes"] == 90.0 and by_site["S-J"]["scc_minutes"] == 180
    assert avail["unavailable_minutes"] == 1853.0 and avail["scheduled_uptime_minutes"] == 446400
    assert lines[(KPI_REPEAT_FAULT, ALL)].evidence["repeat_sites"] == ["S-A"]


def test_eligible_plus_excluded_always_accounts_for_every_incident_in_the_pool(golden):
    """No incident silently disappears from a line: measured + excluded == the pool."""
    _settings, session, _terms_ = golden
    pool = {"P1": 2, "P2": 4, "P3": 3, "P4": 4, ALL: 13}
    for line in sc.lines_of(session, _card(session).id):
        expected = 14 if line.kpi == KPI_AVAILABILITY else pool[line.priority]  # availability's pool also holds G00
        assert line.eligible_incidents + line.excluded_incidents == expected, (line.kpi, line.priority)


def test_only_the_red_vendor_level_line_carries_a_credit_and_it_is_only_proposed(golden):
    _settings, session, _terms_ = golden
    lines = sc.lines_of(session, _card(session).id)
    credited = [(l.kpi, l.priority, l.proposed_credit_pct, l.credit_status) for l in lines if l.credit_status != CREDIT_NONE]
    assert credited == [(KPI_SLA_COMPLIANCE, ALL, 15.0, CREDIT_PROPOSED)]  # escalating [15, 30, 50], first RED period
    line = next(l for l in lines if l.credit_status == CREDIT_PROPOSED)
    assert "PROPOSED" in line.formula and "defaults, not contract" in line.formula
    assert line.evidence["credit"]["consecutive_prior_red_periods"] == []
    assert all(l.proposed_credit_pct is None for l in lines if l.credit_status == CREDIT_NONE)


def test_every_line_has_a_formula_with_its_own_operands_and_a_yaml_path_that_resolves(golden):
    """Every part of every ``yaml_path`` resolves through ``SlaTerms.resolve_path`` (the file)
    -- and the whole through ``resolve_term``. A banded line also cites the thresholds it was
    banded against; an all-priorities line cites all four limits, not a parent mapping."""
    settings, session, terms = golden
    lines = sc.lines_of(session, _card(session).id)
    for line in lines:
        assert line.formula.strip() and line.kpi in KPIS
        sc.resolve_term(terms, settings.operator, line.yaml_path)  # KeyError = a number nobody can trace to a term
        for part in line.yaml_path.split(";"):
            terms.resolve_path(part)  # every part is a real key in the terms FILE for this fixture
        assert "ack_minutes" not in line.yaml_path and "restore_minutes" not in line.yaml_path
    by_key = {(l.kpi, l.priority): l for l in lines}
    assert by_key[(KPI_MTTA, "P1")].yaml_path == "sla_terms.default.P1.ack"
    assert by_key[(KPI_ADJ_MTTR, "P2")].yaml_path == "sla_terms.default.P2.restore"
    assert by_key[(KPI_SLA_COMPLIANCE, "P3")].yaml_path == "sla_terms.default.P3.restore;scorecards.bands.SLA_COMPLIANCE_PCT"
    assert by_key[(KPI_NOTE_COMPLIANCE, "P4")].yaml_path == "sla_terms.default.P4.note_interval;scorecards.bands.NOTE_COMPLIANCE_PCT"
    assert by_key[(KPI_SLA_COMPLIANCE, ALL)].yaml_path == (
        "sla_terms.default.P1.restore;sla_terms.default.P2.restore;sla_terms.default.P3.restore;sla_terms.default.P4.restore;"
        "scorecards.bands.SLA_COMPLIANCE_PCT"
    )  # the credit-bearing line names every term it was judged against
    assert by_key[(KPI_MTTA, ALL)].yaml_path == "sla_terms.default.P1.ack;sla_terms.default.P2.ack;sla_terms.default.P3.ack;sla_terms.default.P4.ack"
    assert by_key[(KPI_REPEAT_FAULT, ALL)].yaml_path == "scorecards.period"
    assert by_key[(KPI_AVAILABILITY, ALL)].yaml_path == "scorecards.bands.AVAILABILITY_PCT;sla_terms.default.availability_target_pct"
    assert sc.resolve_term(terms, settings.operator, by_key[(KPI_SLA_COMPLIANCE, "P1")].yaml_path) == {
        "sla_terms.default.P1.restore": 60,
        "scorecards.bands.SLA_COMPLIANCE_PCT": {"green": 95, "amber": 90},
    }
    assert sc.resolve_term(terms, settings.operator, "sla_terms.default.P1.restore") == 60
    assert "100 x 5 compliant / 10 eligible = 50 %" in by_key[(KPI_SLA_COMPLIANCE, ALL)].formula
    assert "band: GREEN >= 95, AMBER >= 90, else RED (scorecards.bands.SLA_COMPLIANCE_PCT)" in by_key[(KPI_SLA_COMPLIANCE, ALL)].formula
    assert "100 x 33 met / 41 expected note slots = 80.49 %" in by_key[(KPI_NOTE_COMPLIANCE, ALL)].formula
    assert "24 x 60 x 31 days x 10 site(s) = 446400 min" in by_key[(KPI_AVAILABILITY, ALL)].formula
    assert "AFFECTED SITES ONLY" in by_key[(KPI_AVAILABILITY, ALL)].formula and "band NA" in by_key[(KPI_AVAILABILITY, ALL)].formula


def test_recomputing_the_same_period_against_the_same_terms_gives_the_same_lines(golden):
    settings, session, terms = golden
    card = _card(session)
    before = [(l.id, sc.line_out(l)) for l in sc.lines_of(session, card.id)]
    first_run, dq, discipline, tj = card.computed_by_run_id, card.data_quality_json, card.discipline_json, card.terms_json
    second_run = _run(session)
    sc.compute_period(session, settings.operator, PERIOD, run_id=second_run, terms=terms, now=NOW + timedelta(days=30))
    session.commit()
    session.expire_all()
    again = _card(session)
    assert again.id == card.id and again.computed_by_run_id == second_run != first_run
    assert [(l.id, sc.line_out(l)) for l in sc.lines_of(session, again.id)] == before  # same ids, same digits, same formulas
    assert (again.data_quality_json, again.discipline_json, again.terms_json) == (dq, discipline, tj)
    assert len(sc.lines_of(session, again.id)) == 22


def test_nothing_is_published_or_queued_by_computing(golden):
    """No auto-send, no auto-publish: a computation leaves no outbox row and no broadcast."""
    _settings, session, _terms_ = golden
    assert session.scalars(select(OutboxRow)).all() == []
    assert session.scalars(select(BroadcastRow)).all() == []
    assert {c.status for c in session.scalars(select(VendorScorecardRow))} <= {"DRAFT", "SHADOW", "WITHHELD"}


def test_other_vendors_operators_and_periods_do_not_leak_into_the_card(golden):
    _settings, session, _terms_ = golden
    lines = sc.lines_of(session, _card(session).id)
    named = lambda l: {row["incident"] for row in l.evidence.get("measured", []) + l.evidence.get("excluded", [])} | {n for site in l.evidence.get("sites", []) for n in site["incidents"]}  # noqa: E731
    august = {f"G{i:02d}" for i in range(1, 14)}
    for line in lines:
        if line.priority is None and line.kpi != KPI_AVAILABILITY:
            assert named(line) == august, line.kpi  # every August incident is accounted for, and nothing else
    assert named(next(l for l in lines if l.kpi == KPI_AVAILABILITY)) == august | {"G00"}  # July's incident, August's minutes
    assert not {n for l in lines for n in named(l)} & {"N-SEPT", "N-TETRA", "N-AIRTEL"}
    tetranet = _card(session, "TETRANET")  # its own card, its own single incident
    assert {(l.kpi, l.priority): l.eligible_incidents for l in sc.lines_of(session, tetranet.id)}[(KPI_ADJ_MTTR, ALL)] == 1
    # N-SEPT failed at 21:00Z = 00:00 EAT on 1 September: it is September's, in every KPI
    september = sc.gather_facts(session, _settings.operator, session.get(sc.VendorRow, _card(session).vendor_id), sc.period_bounds("2026-09"))
    assert [f.incident_number for f in september[0]] == ["N-SEPT"]
    # G12 is still down in September; G09 (cancelled, never restored) is fetched too and is then excluded by name
    assert [f.incident_number for f in september[1]] == ["G09", "G12", "N-SEPT"]


# --------------------------------------------------------------------------
# Degrading honestly; contracted scope
# --------------------------------------------------------------------------


def test_with_the_maintenance_lane_off_planned_windows_are_not_excluded_and_the_line_says_so(tmp_db):
    settings, session = tmp_db  # MAINTENANCE_ENABLED is pinned false by conftest
    build_fixture(session)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=_terms(settings.operator), now=NOW)
    card = _card(session)
    line = next(l for l in sc.lines_of(session, card.id) if l.kpi == KPI_AVAILABILITY)
    assert line.raw_value == 99.5715  # 1913 unavailable minutes: S-I keeps the 60 the window would have excused
    assert "NOT excluded" in line.formula and "MAINTENANCE_ENABLED is off" in line.formula
    assert line.evidence["planned_windows_excluded"] is False and card.terms["planned_windows_excluded"] is False
    assert {row["site_id"]: row["planned_minutes"] for row in line.evidence["sites"]}["S-I"] == 0
    # every other line is untouched by that lane
    got = _numbers(session, card)
    assert {k: v for k, v in got.items() if k[0] != KPI_AVAILABILITY} == {k: v for k, v in GOLDEN.items() if k[0] != KPI_AVAILABILITY}


def _custom_terms(tmp_path, cfg, mutate):
    raw = yaml.safe_load(DEFAULT_SLA_TERMS_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "terms.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_sla_terms(path, cfg=cfg)


def test_a_contracted_site_count_is_used_cited_and_banded(tmp_db, tmp_path, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["vendors"]["EGYPRO"].update(sites_in_scope=40))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    line = next(l for l in sc.lines_of(session, _card(session).id) if l.kpi == KPI_AVAILABILITY)
    assert (line.raw_value, line.band) == (99.8962, BAND_GREEN)  # 100 x (1 785 600 - 1853) / 1 785 600
    assert "sla_terms.vendors.EGYPRO.sites_in_scope" in line.formula and "band NA" not in line.formula
    assert terms.resolve_path("sla_terms.vendors.EGYPRO.sites_in_scope") == 40


def test_a_contracted_scope_smaller_than_the_affected_sites_is_refused_not_banded(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["vendors"]["EGYPRO"].update(sites_in_scope=3))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    line = next(l for l in sc.lines_of(session, _card(session).id) if l.kpi == KPI_AVAILABILITY)
    assert line.raw_value is None and line.band == BAND_NA and "smaller than the 10 affected sites" in line.formula


def test_a_band_that_fell_back_to_the_operator_profile_cites_it_and_still_resolves(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["default"].pop("P3"))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    card = _card(session)
    by_key = {(l.kpi, l.priority): l for l in sc.lines_of(session, card.id)}
    assert by_key[(KPI_SLA_COMPLIANCE, "P3")].yaml_path == "sla_minutes.P3.restore;scorecards.bands.SLA_COMPLIANCE_PCT"
    assert sc.resolve_term(terms, settings.operator, "sla_minutes.P3.restore") == 240
    assert by_key[(KPI_SLA_COMPLIANCE, ALL)].yaml_path == (  # mixed sources: each priority cites its own origin
        "sla_terms.default.P1.restore;sla_terms.default.P2.restore;sla_minutes.P3.restore;sla_terms.default.P4.restore;scorecards.bands.SLA_COMPLIANCE_PCT"
    )
    assert "restore limit 240 min (sla_minutes.P3.restore -- operator profile, not contract)" in by_key[(KPI_SLA_COMPLIANCE, "P3")].formula
    assert card.terms["bands_from_operator_profile"] == ["P3"]
    for line in by_key.values():
        sc.resolve_term(terms, settings.operator, line.yaml_path)
    assert _numbers(session, card)[(KPI_SLA_COMPLIANCE, "P3")] == GOLDEN[(KPI_SLA_COMPLIANCE, "P3")]  # same minutes, same number


def test_when_every_band_falls_back_to_the_profile_every_yaml_path_still_resolves(tmp_db, tmp_path, monkeypatch):
    """The reviewers' case: all four priorities missing from the terms file. The all-priority
    lines used to cite a bare ``sla_minutes`` that nothing could resolve -- and one of them is
    the line that carries the proposed credit."""
    settings, session = tmp_db
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")  # the golden table was computed with the window excluded
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: [raw["sla_terms"]["default"].pop(p) for p in ("P1", "P2", "P3", "P4")])
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    lines = sc.lines_of(session, _card(session).id)
    assert len(lines) == 22
    for line in lines:
        resolved = sc.resolve_term(terms, settings.operator, line.yaml_path)
        assert resolved not in (None, {}), (line.kpi, line.priority, line.yaml_path)
    by_key = {(l.kpi, l.priority): l for l in lines}
    credit_line = by_key[(KPI_SLA_COMPLIANCE, ALL)]
    assert credit_line.credit_status == "PROPOSED"
    assert credit_line.yaml_path == "sla_minutes.P1.restore;sla_minutes.P2.restore;sla_minutes.P3.restore;sla_minutes.P4.restore;scorecards.bands.SLA_COMPLIANCE_PCT"
    assert sc.resolve_term(terms, settings.operator, credit_line.yaml_path)["sla_minutes.P1.restore"] == 60
    assert sc.resolve_term(terms, settings.operator, "sla_minutes") == {
        p: {"ack": b.ack, "restore": b.restore, "note_interval": b.note_interval} for p, b in settings.operator.sla_minutes.items()
    }
    assert _numbers(session, _card(session)) == GOLDEN  # the profile holds the same minutes: same numbers
    with pytest.raises(KeyError):
        sc.resolve_term(terms, settings.operator, "sla_minutes.P9.restore")
    with pytest.raises(KeyError):
        sc.resolve_term(terms, settings.operator, "sla_terms.default.P1.restore;nothing.here")


def test_the_word_contract_appears_only_for_a_real_vendor_block(tmp_db, tmp_path):
    """Per-priority formulas name the origin of their term. The shipped file has synthetic
    vendor blocks and default bands, so no line may say "contract ack target" -- that text is
    what the vendor pack renders line by line."""
    settings, session = tmp_db
    build_fixture(session)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=_terms(settings.operator), now=NOW, vendor_code="EGYPRO")
    by_key = {(l.kpi, l.priority): l for l in sc.lines_of(session, _card(session).id)}
    assert "ack target 5 min (sla_terms.default.P1.ack -- defaults, not contract)" in by_key[(KPI_MTTA, "P1")].formula
    assert "restore limit 120 min (sla_terms.default.P2.restore -- defaults, not contract)" in by_key[(KPI_SLA_COMPLIANCE, "P2")].formula
    assert "note interval 60 min (sla_terms.default.P3.note_interval -- defaults, not contract)" in by_key[(KPI_NOTE_COMPLIANCE, "P3")].formula
    for line in by_key.values():  # "contracted site list" on the availability line is about scope, not a term's origin
        text = line.formula.replace("not contract", "").replace("SYNTHETIC contract", "").replace("sample contract", "").replace("contracted", "")
        assert "contract" not in text, (line.kpi, line.priority, line.formula)

    # a SYNTHETIC vendor block that overrides a band is named as such...
    synthetic = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["vendors"]["EGYPRO"].update(P1={"ack": 4, "restore": 45, "note_interval": 10}))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=synthetic, now=NOW, vendor_code="EGYPRO")
    line = {(l.kpi, l.priority): l for l in sc.lines_of(session, _card(session).id)}[(KPI_SLA_COMPLIANCE, "P1")]
    assert "restore limit 45 min (sla_terms.vendors.EGYPRO.P1.restore -- SYNTHETIC sample contract, not an agreement anyone signed)" in line.formula
    assert "contract restore limit" not in line.formula

    # ...and only a NON-synthetic vendor block earns the word
    def real(raw):
        raw["sla_terms"]["vendors"]["EGYPRO"].update(P1={"ack": 4, "restore": 45, "note_interval": 10}, contract_ref="MSA-2026-EGYPRO-01", contract_is_synthetic=False)

    contract = _custom_terms(tmp_path, settings.operator, real)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=contract, now=NOW, vendor_code="EGYPRO")
    card = _card(session)
    line = {(l.kpi, l.priority): l for l in sc.lines_of(session, card.id)}[(KPI_SLA_COMPLIANCE, "P1")]
    assert "contract restore limit 45 min (sla_terms.vendors.EGYPRO.P1.restore, contract MSA-2026-EGYPRO-01)" in line.formula
    assert card.terms["basis"] == "CONTRACT"


def test_availability_cites_its_band_thresholds_and_says_when_the_target_disagrees(tmp_db, tmp_path, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    build_fixture(session)

    def disagree(raw):
        raw["sla_terms"]["default"]["availability_target_pct"] = 99.9
        raw["sla_terms"]["vendors"]["EGYPRO"]["sites_in_scope"] = 40

    terms = _custom_terms(tmp_path, settings.operator, disagree)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    line = next(l for l in sc.lines_of(session, _card(session).id) if l.kpi == KPI_AVAILABILITY)
    assert line.yaml_path == "scorecards.bands.AVAILABILITY_PCT;sla_terms.default.availability_target_pct"
    assert (line.raw_value, line.band) == (99.8962, BAND_GREEN)  # banded on scorecards.bands (green 99.5), not on the 99.9 target
    assert "band: GREEN >= 99.5, AMBER >= 99.0, else RED (scorecards.bands.AVAILABILITY_PCT)" in line.formula
    assert "availability_target_pct (99.9) and scorecards.bands.AVAILABILITY_PCT.green (99.5) disagree" in line.formula
    # no bands block at all: the target alone is cited and the line is NA
    none = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].pop("bands"))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=none, now=NOW, vendor_code="EGYPRO")
    line = next(l for l in sc.lines_of(session, _card(session).id) if l.kpi == KPI_AVAILABILITY)
    assert line.yaml_path == "sla_terms.default.availability_target_pct" and line.band == BAND_NA


def test_an_open_incident_is_a_normalised_breach_only_if_its_normalised_time_exceeds_the_limit(tmp_db):
    """Reading 3 applied to the normalised figure. A P2 in CST (x1.25) still open at 130
    adjusted minutes: a RAW breach (130 > 120), but 130 / 1.25 = 104 <= 120, so its normalised
    outcome is unknown at the period end and it is left out of the normalised denominator."""
    settings, session = tmp_db
    terms = _terms(settings.operator)
    fail = datetime(2026, 8, 31, 18, 50)  # 130 min before the period end at 21:00Z
    _incident(session, "OPEN-CST", "P2", "C-1", "CST", "POWER", fail, fail + timedelta(minutes=2), fail + timedelta(minutes=5), None, None, {"status": "IN_PROGRESS"})
    _incident(session, "DONE-NBI", "P2", "C-2", "NBI_E", "POWER", _t(4, 9), _t(4, 9, 2), _t(4, 9, 10), _t(4, 10), "MARK_RESTORED", {})  # 60 min, compliant
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    line = {(l.kpi, l.priority): l for l in sc.lines_of(session, _card(session).id)}[(KPI_SLA_COMPLIANCE, "P2")]
    assert (line.raw_value, line.normalised_value, line.eligible_incidents) == (50.0, 100.0, 2)  # raw: 1 of 2; normalised: 1 of 1
    rows = {r["incident"]: r for r in line.evidence["measured"]}
    assert rows["OPEN-CST"]["kind"] == "OPEN_BREACHED" and rows["OPEN-CST"]["compliant"] is False
    assert rows["OPEN-CST"]["in_normalised_denominator"] is False and rows["OPEN-CST"]["compliant_normalised"] is False
    assert "an open incident is a normalised breach only if its normalised elapsed exceeds the limit, else left out" in line.formula
    # the same incident two hours later IS a normalised breach: 250 / 1.25 = 200 > 120
    for inc in session.scalars(select(IncidentRow).where(IncidentRow.incident_number == "OPEN-CST")):
        inc.failure_time = inc.created_at = datetime(2026, 8, 31, 16, 50)
    session.flush()
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    line = {(l.kpi, l.priority): l for l in sc.lines_of(session, _card(session).id)}[(KPI_SLA_COMPLIANCE, "P2")]
    assert (line.raw_value, line.normalised_value) == (50.0, 50.0)


def test_missing_policy_knobs_stop_the_computation_rather_than_being_guessed(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].pop("max_inferred_restore_pct"))
    with pytest.raises(ValueError, match="max_inferred_restore_pct"):
        sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    quarterly = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].update(period="QUARTER"))
    with pytest.raises(ValueError, match="QUARTER"):
        sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=quarterly, now=NOW, vendor_code="EGYPRO")


def test_a_kpi_with_no_configured_band_is_na_never_an_invented_threshold(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"]["bands"].pop("NOTE_COMPLIANCE_PCT"))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    got = _numbers(session, _card(session))
    assert got[(KPI_NOTE_COMPLIANCE, ALL)][:4] == (80.49, None, None, BAND_NA)


# --------------------------------------------------------------------------
# Discipline counters; the paired metric
# --------------------------------------------------------------------------


def test_discipline_counters_report_the_operator_and_never_move_a_vendor_number(tmp_db, tmp_path, monkeypatch):
    """``opened_at - started_at`` is how late OUR NOC recorded a stop clock. Same conditions,
    sloppier paperwork: the counter changes, and not one digit of any line does."""
    settings, session = tmp_db
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    build_fixture(session, late_openings=True)
    terms = _terms(settings.operator)
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    late_card = _card(session)
    late_lines = [sc.line_out(l) for l in sc.lines_of(session, late_card.id)]
    d = late_card.discipline
    assert d["late_scc_openings"] == 2 and d["scc_events"] == 7 and d["late_scc_opening_threshold_min"] == 60
    assert [(e["incident"], e["scc_code"], e["opening_delay_min"], e["reversed"]) for e in d["late_scc_opening_events"]] == [
        ("G02", "OBSERVATION", 75, True),  # late even though later reversed
        ("G04", "UTILITY_POWER", 90, False),
    ]
    # "we did not look" is not "none found"
    assert d["missing_scc_with_confirmed_power"] is None and d["missing_scc_with_confirmed_power_status"] == "NOT_COMPUTABLE"

    for ev in session.scalars(select(ClockEventRow)):
        ev.opened_at = ev.started_at + timedelta(minutes=1)  # the same clocks, recorded promptly
    session.flush()
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    session.expire_all()
    prompt_card = _card(session)
    assert prompt_card.discipline["late_scc_openings"] == 0
    assert [sc.line_out(l) for l in sc.lines_of(session, prompt_card.id)] == late_lines


def test_the_discipline_threshold_comes_from_the_terms_file(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].update(late_scc_opening_minutes=80))
    sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code="EGYPRO")
    d = _card(session).discipline
    assert (d["late_scc_openings"], d["late_scc_opening_threshold_min"]) == (1, 80)  # only G04's 90 minutes


def test_paired_metric_closing_early_buys_mttr_and_pays_for_it_in_repeat_faults(tmp_db):
    """§7.6.8: a vendor that closes a ticket before the fault is fixed gets a better ADJ_MTTR --
    and the fault comes back at the same site under the same signature, so REPEAT_FAULT_RATE
    rises. The two KPIs are read together precisely so that gaming one shows up in the other."""
    settings, session = tmp_db
    terms = _terms(settings.operator)

    def card_for(number_prefix: str, site_suffix: str, early_close: bool) -> dict:
        base = _t(4, 8)
        honest = [(f"{number_prefix}1", f"H{site_suffix}-1", base, base + timedelta(minutes=200)), (f"{number_prefix}2", f"H{site_suffix}-2", base + timedelta(days=1), base + timedelta(days=1, minutes=100))]
        rows = honest
        if early_close:  # site 1 "restored" after 40 min, fails again four hours later, fixed properly the second time
            rows = [
                (f"{number_prefix}1", f"H{site_suffix}-1", base, base + timedelta(minutes=40)),
                (f"{number_prefix}3", f"H{site_suffix}-1", base + timedelta(hours=4), base + timedelta(hours=4, minutes=160)),
                honest[1],
            ]
        return rows

    def compute(rows, vendor: str) -> dict:
        for number, site, failure, restored in rows:
            _incident(session, number, "P3", site, "NBI_E", "POWER", failure, failure + timedelta(minutes=5), failure + timedelta(minutes=10), restored, "MARK_RESTORED", {}, msp=vendor)
        sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW, vendor_code=vendor)
        return {(l.kpi, l.priority): l.raw_value for l in sc.lines_of(session, _card(session, vendor).id)}

    honest = compute(card_for("HON", "a", early_close=False), "EGYPRO")
    gamed = compute(card_for("GAM", "b", early_close=True), "TETRANET")
    assert (honest[(KPI_ADJ_MTTR, ALL)], honest[(KPI_REPEAT_FAULT, ALL)]) == (150.0, 0.0)  # median(100, 200); 0 of 2 sites
    assert (gamed[(KPI_ADJ_MTTR, ALL)], gamed[(KPI_REPEAT_FAULT, ALL)]) == (100.0, 0.5)  # median(40, 100, 160); 1 of 2 sites
    assert gamed[(KPI_ADJ_MTTR, ALL)] < honest[(KPI_ADJ_MTTR, ALL)] and gamed[(KPI_REPEAT_FAULT, ALL)] > honest[(KPI_REPEAT_FAULT, ALL)]


# --------------------------------------------------------------------------
# The primitives (pure: no database)
# --------------------------------------------------------------------------

P_AUG = sc.period_bounds(PERIOD)


def _ev(start, end, *, reversed_=False, delay=5, code="UTILITY_POWER", id_="ev"):
    return ClockEventRow(id=id_, incident_id="x", scc_code=code, started_at=start, ended_at=end, opened_by="a", opened_role="noc_analyst", opened_at=start + timedelta(minutes=delay), reason="r", reversed_at=_t(30, 0) if reversed_ else None, created_at=start)


def _facts(**kw) -> sc.IncidentFacts:
    base = dict(incident_id=kw.get("incident_number", "F1"), incident_number="F1", priority="P1", status="RESTORED", site_id="S", region_code="NBI_E", failure_domain="POWER", started_at=_t(3, 6), restored_source="MARK_RESTORED")
    base.update(kw)
    return sc.IncidentFacts(**base)


def test_the_period_is_the_eat_calendar_month_in_naive_utc():
    p = sc.period_bounds("2026-08")
    assert (p.start, p.end, p.days, p.minutes) == (datetime(2026, 7, 31, 21), datetime(2026, 8, 31, 21), 31, 44640)
    assert p.end - p.start == timedelta(minutes=p.minutes)
    feb = sc.period_bounds("2028-02")
    assert (feb.days, feb.minutes, feb.end) == (29, 41760, datetime(2028, 2, 29, 21))  # leap year
    dec = sc.period_bounds("2026-12")
    assert dec.end == datetime(2026, 12, 31, 21) and sc.previous_period("2026-01") == "2025-12"
    for bad in ("2026-13", "2026-8", "26-08", "2026", "", "2026-00", "0001-01", "+026-09", "2026-09x", "2026-09;", "1999-12", "2101-01", "\uff12\uff10\uff12\uff16-08", "2026-0\u0669"):
        with pytest.raises(ValueError):  # never an OverflowError, never a Unicode digit, never year 26
            sc.period_bounds(bad)
    assert sc.period_bounds(" 2026-08 ").label == "2026-08"  # surrounding whitespace is the only leniency
    # 22:30Z on 31 August is 01:30 EAT on 1 September: August has ended
    assert sc.last_ended_period(datetime(2026, 8, 31, 22, 30)) == "2026-08"
    assert sc.last_ended_period(datetime(2026, 8, 31, 20, 59)) == "2026-07"


def test_rounding_is_half_up_and_exact():
    assert sc._round(Fraction(57125, 1000), 2) == 57.13  # float round() gives 57.12
    assert sc._round(Fraction(1, 9), 4) == 0.1111 and sc._round(Fraction(2, 3) * 100, 2) == 66.67
    assert sc._round(Fraction(5, 1000), 2) == 0.01 and sc._round(Fraction(4999, 1000000), 2) == 0.0
    assert sc._round(None, 2) is None
    assert sc._median([Fraction(3), Fraction(4)]) == Fraction(7, 2) and sc._median([]) is None


def test_overlapping_stop_clocks_deduct_their_union_and_that_decides_the_p1_breach():
    events = (_ev(_t(3, 6, 10), _t(3, 6, 40), id_="a"), _ev(_t(3, 6, 30), _t(3, 7), id_="b", code="SITE_ACCESS_DENIED"))
    minutes, scc, why = sc.adjusted_restore(_facts(restored_at=_t(3, 8), clock_events=events))
    assert (minutes, scc, why) == (Fraction(70), 50, None)  # the SUM would be 60 -> 60 min -> a pass. It is a breach.


def test_a_reversed_clock_deducts_nothing_and_an_untrusted_restore_is_not_measured():
    reversed_ = (_ev(_t(3, 6, 5), _t(3, 6, 35), reversed_=True),)
    assert sc.adjusted_restore(_facts(restored_at=_t(3, 6, 45), clock_events=reversed_))[:2] == (Fraction(45), 0)
    for source in ("VENDOR_NOTE_INFERRED", "ALARM_CLEAR", None, "", "mark_restored"):
        minutes, _scc, why = sc.adjusted_restore(_facts(restored_at=_t(3, 7), restored_source=source))
        assert minutes is None and why.startswith("UNTRUSTED_RESTORE_SOURCE:")
    assert sc.adjusted_restore(_facts(restored_at=None))[2] == "NOT_RESTORED"
    assert sc.adjusted_restore(_facts(restored_at=_t(3, 5)))[2] == "RESTORED_BEFORE_START"


def test_a_stop_clock_left_open_is_bounded_by_the_period_end():
    f = _facts(started_at=_t(31, 15), restored_at=None, restored_source=None, clock_events=(_ev(_t(31, 18), None),))
    assert sc.adjusted_elapsed_at(f, P_AUG.end) == (Fraction(180), 180)  # 360 elapsed, clock 18:00-21:00
    # ...and when the incident IS restored, by the restore -- never by "now"
    assert sc.adjusted_restore(_facts(started_at=_t(31, 15), restored_at=_t(31, 19), clock_events=(_ev(_t(31, 18), None),)))[:2] == (Fraction(180), 60)


def test_note_slots_are_set_by_the_clock_so_posting_more_cannot_inflate_them():
    esc = _t(3, 6)
    def slots(offsets, *, end_min, interval=15, mult=Fraction(1)):
        f = _facts(escalated_at=esc, restored_at=esc + timedelta(minutes=end_min), region_multiplier=mult, vendor_note_times=tuple(esc + timedelta(minutes=m) for m in offsets))
        s = sc.note_slots(f, note_interval=interval, period_end=P_AUG.end)
        return s.met, s.expected
    assert slots([4, 18, 33, 48, 78, 98, 116], end_min=118) == (6, 7)
    assert slots(list(range(1, 13)), end_min=180) == (1, 12)  # twelve notes in twelve minutes, then silence: ONE slot
    assert slots([15, 30, 45], end_min=45) == (3, 3)  # a note exactly on the boundary meets the slot it closes
    assert slots([0], end_min=20) == (1, 1) and slots([16], end_min=20) == (0, 1)
    assert slots([5], end_min=14) == (0, 0)  # restored before the first update was due: nothing was expected
    assert slots([12, 66, 100], end_min=133, interval=30, mult=Fraction(23, 20)) == (3, 3)  # 34.5-minute slots
    assert slots([12, 66, 100], end_min=133, interval=30) == (3, 4)  # without the allowance (30,60] is empty
    assert sc.note_slots(_facts(escalated_at=None), note_interval=15, period_end=P_AUG.end) is None
    # notes before escalation and after the restore belong to no slot
    assert slots([-5, 200], end_min=60) == (0, 4)


def test_site_unavailability_counts_each_site_minute_once_and_excuses_only_the_stopped_incident():
    a = _facts(incident_number="A", started_at=_t(3, 6), restored_at=_t(3, 8), clock_events=(_ev(_t(3, 6, 30), _t(3, 7, 30)),))
    single = sc.site_unavailability("S", [a], P_AUG, None)
    assert (single.gross_minutes, single.scc_minutes, single.unavailable_minutes) == (120, 60, 60)
    assert single.scc_minutes == deducted_minutes(a.clock_events, window_start=a.started_at, window_end=a.restored_at)
    # B overlaps A's stopped hour and has no stop clock of its own: those minutes are still owed
    b = _facts(incident_number="B", started_at=_t(3, 7), restored_at=_t(3, 9))
    both = sc.site_unavailability("S", [a, b], P_AUG, None)
    assert (both.gross_minutes, both.scc_minutes, both.unavailable_minutes) == (180, 30, 150)  # only 06:30-07:00 is excused
    # planned minutes are looked up over what no stop clock already covers -> excluded ONCE
    asked = []
    def planned(site, start, end):
        asked.append((start, end))
        return 10
    out = sc.site_unavailability("S", [a], P_AUG, planned)
    assert asked == [(_t(3, 6), _t(3, 6, 30)), (_t(3, 7, 30), _t(3, 8))] and out.planned_minutes == 20 and out.unavailable_minutes == 40
    # clipping: an outage straddling the period start, and one that never restored
    tail = _facts(incident_number="T", started_at=datetime(2026, 7, 31, 20, 30), restored_at=datetime(2026, 7, 31, 22, 30))
    assert sc.site_unavailability("S", [tail], P_AUG, None).unavailable_minutes == 90
    open_ = _facts(incident_number="O", started_at=_t(31, 15), restored_at=None, clock_events=(_ev(_t(31, 18), None),))
    assert sc.site_unavailability("S", [open_], P_AUG, None).unavailable_minutes == 180


def test_bands_are_inclusive_at_the_threshold_and_na_without_one():
    bands = {"SLA_COMPLIANCE_PCT": {"green": 95, "amber": 90}, "MTTA_MIN": {"green": 5, "amber": 10}}
    assert [sc.band_for(KPI_SLA_COMPLIANCE, v, bands) for v in (100.0, 95.0, 94.99, 90.0, 89.99, 0.0)] == [BAND_GREEN, BAND_GREEN, BAND_AMBER, BAND_AMBER, BAND_RED, BAND_RED]
    assert sc.band_for(KPI_SLA_COMPLIANCE, None, bands) == BAND_NA
    assert sc.band_for(KPI_NOTE_COMPLIANCE, 50.0, bands) == BAND_NA  # not configured: no invented threshold
    assert sc.band_for(KPI_MTTA, 3.0, bands) == BAND_NA  # lower-is-better: would be read upside down, so not read
    assert sc.band_for(KPI_SLA_COMPLIANCE, 99.0, {"SLA_COMPLIANCE_PCT": {"green": 95}}) == BAND_NA


def test_repeat_faults_count_sites_not_visits_and_need_the_same_signature():
    f = lambda n, site, domain: _facts(incident_number=n, incident_id=n, site_id=site, failure_domain=domain)  # noqa: E731
    sites, repeats = sc.repeat_fault_sites([f("1", "A", "POWER"), f("2", "A", "POWER"), f("3", "A", "POWER"), f("4", "B", "POWER"), f("5", "B", "TRANSMISSION")])
    assert (sites, repeats) == (["A", "B"], ["A"])  # three visits to A is ONE repeat site; B's two differ in domain


def test_mtta_needs_both_timestamps_in_order():
    assert sc.mtta_minutes(_facts(escalated_at=_t(3, 6, 2), first_vendor_note_at=_t(3, 6, 6))) == (Fraction(4), None)
    assert sc.mtta_minutes(_facts(escalated_at=None))[1] == "NOT_ESCALATED"
    assert sc.mtta_minutes(_facts(escalated_at=_t(3, 6)))[1] == "NO_VENDOR_NOTE"
    assert sc.mtta_minutes(_facts(escalated_at=_t(3, 6), first_vendor_note_at=_t(3, 5)))[1] == "NOTE_BEFORE_ESCALATION"
