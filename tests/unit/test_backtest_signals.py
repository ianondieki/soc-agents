"""The §10.6 early-warning backtest and ``signal_precision_30d`` (CONFORMANCE C-14).

No network and no pollers: signal rows are written directly, shaped exactly as the pollers
write them, so each test can say "twelve storm episodes, nine followed by an incident" and
mean it.

What is pinned:

* **a flag is an episode, not a row** — twelve 15-minute rows of one storm are one episode,
  so precision does not depend on the polling cadence;
* **not enough data is an answer** (the ``services/capacity.py`` precedent): under 90 days of
  history (§10.6's own floor) or under 10 resolved episodes the verdict is
  ``INSUFFICIENT_DATA`` and the number is withheld — ``None``, never 0 — while the counts are
  still reported;
* ``LOW_CONFIDENCE`` below ``min_precision`` (0.2) publishes the number and labels it, as §10.6
  says ("rather than hiding it");
* an episode whose claim has not closed is pending, not a miss;
* Wilson intervals, lift and median lead time;
* both operators seeded into one region code; neither's rows move the other's score;
* the dashboard hook's signature fits ``services/dashboards._weather_block`` as it stands;
* the script runs on an empty database and on a seeded one.

Review findings pinned here: F05 (overall row pools only regions that pass the floors), F11
(incidents loaded to the claim's end), F12 (region-hour lift), F13 (lift and lead withheld with
precision), F14 (a calm reading cuts a storm claim), F19 (history measured to the window's end).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from noc_agents.db.models import ExternalSignalRow, IncidentRow, new_id
from noc_agents.services import backtest
from noc_agents.services import dashboards
from noc_agents.services.backtest import (
    Episode,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_LOW_CONFIDENCE,
    VERDICT_MEASURED,
    build_episodes,
    precision_verdict_30d,
    replay,
    score_region,
    signal_precision_30d,
    wilson_interval,
)

NOW = datetime(2026, 9, 18, 12, 0)
REGION = "WNY"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest_signals.py"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a backtest test tried to open a network socket")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# ------------------------------------------------------------------------------ writers


class Seed:
    def __init__(self, session) -> None:
        self.session = session
        self.n = 0

    def weather(self, at: datetime, *, storm: bool, region: str = REGION, operator: str = "safaricom", horizon: int = 6) -> None:
        """One forecast row, as ``pollers/weather.py`` writes it (valid 1 h, claims 6 h)."""
        self.n += 1
        self.session.add(ExternalSignalRow(
            id=new_id(), operator_id=operator, source="OPEN_METEO", source_url="mock://", region_code=region,
            fetched_at=at, valid_from=at, valid_until=at + timedelta(hours=1), stale=0, confidence=1.0,
            storm_flag=1 if storm else 0, flood_flag=0, planned_power=0, access_risk=0, payload_json="{}",
            derived_json=json.dumps({"storm_flag": storm, "horizon_hours": horizon}, separators=(",", ":")),
            external_id=f"{region}:{at:%Y-%m-%dT%H:%M}Z#{self.n}", created_at=at,
        ))

    def storm(self, start: datetime, *, rows: int = 4, **kw) -> None:
        """A storm warning held for ``rows`` consecutive 15-minute polls."""
        for i in range(rows):
            self.weather(start + timedelta(minutes=15 * i), storm=True, **kw)

    def incident(self, at: datetime, *, region: str = REGION, operator: str = "safaricom") -> None:
        self.n += 1
        self.session.add(IncidentRow(
            id=new_id(), operator_id=operator, incident_number=f"{operator[:3].upper()}{self.n:07d}",
            site_id=f"SITE-{self.n}", region_code=region, correlation_fingerprint=f"fp-{self.n}",
            failure_time=at, created_at=at, updated_at=at,
        ))

    def history(self, days: int = 100, *, region: str = REGION, operator: str = "safaricom") -> None:
        """The poller has been running this long (one calm row at the start is enough)."""
        self.weather(NOW - timedelta(days=days), storm=False, region=region, operator=operator)

    def commit(self) -> None:
        self.session.commit()


@pytest.fixture()
def seed(tmp_db):
    settings, session = tmp_db
    return Seed(session)


def _storms(seed: Seed, n: int, hits: int, *, operator: str = "safaricom", region: str = REGION) -> None:
    """``n`` separate storm episodes in the last 30 days (two days apart), the first ``hits``
    of them followed by an incident two hours after the flag was first stored."""
    for i in range(n):
        start = NOW - timedelta(days=28) + timedelta(days=2 * i)
        seed.storm(start, operator=operator, region=region)
        if i < hits:
            seed.incident(start + timedelta(hours=2), operator=operator, region=region)


# ------------------------------------------------------------------------------ statistics


def test_wilson_interval_is_honest_at_the_edges():
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.278, abs=1e-3)  # never "0 % ± 0"
    assert wilson_interval(5, 10) == pytest.approx((0.237, 0.763), abs=1e-3)
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.722, abs=1e-3)


def test_the_numbers_the_floor_of_ten_is_argued_from():
    """services/backtest.py calls ten a judgement and quotes these figures; pin them, so the
    argument and the arithmetic cannot drift apart."""

    def widest(n: int) -> float:
        return max((wilson_interval(k, n)[1] - wilson_interval(k, n)[0]) / 2 for k in range(n + 1))

    assert [round(widest(n), 2) for n in (4, 7, 10, 15)] == [0.35, 0.30, 0.26, 0.23]
    assert [round(wilson_interval(0, n)[1], 2) for n in (4, 7, 10)] == [0.49, 0.35, 0.28]
    assert backtest.DEFAULT_BACKTEST["min_episodes"] == 10


# ------------------------------------------------------------------------------ episodes


def test_twelve_rows_of_one_storm_are_one_episode(seed):
    seed.storm(NOW - timedelta(days=3), rows=12)
    seed.commit()
    rows = backtest._family_rows(seed.session, "safaricom", family="storm", since=NOW - timedelta(days=30), until=NOW, now=NOW)
    (episode,) = build_episodes(rows, family="storm")
    assert episode.rows == 12 and episode.start == NOW - timedelta(days=3)
    assert episode.end == NOW - timedelta(days=3) + timedelta(minutes=165) + timedelta(hours=6)  # last row + its 6 h claim


def test_separate_storms_and_separate_regions_stay_separate(seed):
    seed.storm(NOW - timedelta(days=5))
    seed.storm(NOW - timedelta(days=2))
    seed.storm(NOW - timedelta(days=2), region="CST")
    seed.commit()
    rows = backtest._family_rows(seed.session, "safaricom", family="storm", since=NOW - timedelta(days=30), until=NOW, now=NOW)
    episodes = build_episodes(rows, family="storm")
    assert sorted((e.region_code, e.start) for e in episodes) == [
        ("CST", NOW - timedelta(days=2)), (REGION, NOW - timedelta(days=5)), (REGION, NOW - timedelta(days=2))]


# ------------------------------------------------------------------------------ verdicts


def test_an_empty_database_is_insufficient_data_not_zero(seed):
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_INSUFFICIENT_DATA and score["precision"] is None
    assert score["label"] == "precision: not yet measured" and "no stored signal history" in score["reason"]
    assert signal_precision_30d(seed.session, "safaricom", REGION, now=NOW) is None


def test_under_ninety_days_of_history_no_precision_is_published_even_with_plenty_of_episodes(seed):
    seed.history(days=40)
    _storms(seed, 12, hits=12)
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_INSUFFICIENT_DATA and score["precision"] is None
    assert "covers 40 days" in score["reason"] and "90 days" in score["reason"]
    assert (score["episodes_resolved"], score["episodes_hit"]) == (12, 12)  # the counts are still shown
    assert score["floors"]["min_history_days"] == 90


def test_under_ten_resolved_episodes_no_precision_is_published(seed):
    seed.history()
    _storms(seed, 9, hits=9)
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_INSUFFICIENT_DATA and score["precision"] is None
    assert "9 resolved flag episode(s)" in score["reason"] and "at least 10" in score["reason"]
    assert signal_precision_30d(seed.session, "safaricom", REGION, now=NOW) is None


def test_measured_precision_recall_interval_lift_and_lead(seed):
    seed.history()
    _storms(seed, 12, hits=9)
    for day in (1, 7, 13):  # three incidents nobody warned about, in calm weather
        seed.incident(NOW - timedelta(days=day, hours=1))
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_MEASURED and score["label"] is None
    assert score["precision"] == 0.75 and score["precision_ci95"] == list(wilson_interval(9, 12))
    assert (score["incidents"], score["incidents_warned"]) == (12, 9)
    assert score["recall"] == 0.75 and score["recall_verdict"] == VERDICT_MEASURED
    assert score["median_lead_hours"] == 2.0
    assert score["lift"] is not None and score["lift"] > 1  # flagged time really is worse
    assert signal_precision_30d(seed.session, "safaricom", REGION, now=NOW) == 0.75


def test_low_precision_is_published_and_labelled_never_hidden(seed):
    seed.history()
    _storms(seed, 12, hits=1)
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_LOW_CONFIDENCE and score["label"] == "LOW CONFIDENCE"
    assert score["precision"] == pytest.approx(0.083, abs=1e-3) and "shown, greyed, never hidden" in score["reason"]
    assert signal_precision_30d(seed.session, "safaricom", REGION, now=NOW) == pytest.approx(0.083, abs=1e-3)


def test_an_open_episode_is_pending_not_a_false_alarm(seed):
    seed.history()
    _storms(seed, 10, hits=10)
    seed.storm(NOW - timedelta(hours=2))  # still claiming danger until NOW + ~4 h
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert (score["episodes"], score["episodes_resolved"], score["episodes_pending"]) == (11, 10, 1)
    assert score["precision"] == 1.0


def test_min_precision_and_floors_come_from_the_profile_when_it_has_them(seed):
    class Cfg:
        weather = {"min_precision": 0.9, "min_episodes": 3, "not_a_key": 1, "min_history_days": True}

    cfg = backtest.backtest_config(Cfg())
    assert cfg["min_precision"] == 0.9 and cfg["min_episodes"] == 3 and "not_a_key" not in cfg
    assert cfg["min_history_days"] == 90  # a bool is not a number of days
    assert backtest.backtest_config(None) == backtest.DEFAULT_BACKTEST


# ------------------------------------------------------------------------------ operator isolation


def test_the_other_operators_flags_and_incidents_never_move_a_score(seed):
    seed.history()
    _storms(seed, 12, hits=9)
    # Airtel: its own history, twelve storms, every one "hit", in the SAME region code, plus a
    # pile of incidents — if any of it leaked, Safaricom's precision and recall would move.
    seed.history(operator="airtel")
    _storms(seed, 12, hits=12, operator="airtel")
    for day in range(1, 20):
        seed.incident(NOW - timedelta(days=day, hours=5), operator="airtel")
    seed.commit()
    saf = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert (saf["precision"], saf["episodes_resolved"], saf["incidents"]) == (0.75, 12, 9)
    air = precision_verdict_30d(seed.session, "airtel", REGION, now=NOW)
    assert (air["precision"], air["episodes_resolved"]) == (1.0, 12)
    assert air["incidents"] == 12 + 19


# ------------------------------------------------------------------------------ the full replay


def test_replay_reports_every_region_and_family_and_keeps_cap_and_storm_apart(seed):
    seed.history()
    _storms(seed, 12, hits=9)
    # A KMD alert row and its feed-health row: only the alert is a flag, and it is scored as
    # "cap", never blended into the forecast poller's "storm" precision.
    for eid, kind, flag in (("a#WNY", "cap_alert", 1), ("feed:WNY", "cap_feed_health", 0)):
        seed.session.add(ExternalSignalRow(
            id=new_id(), operator_id="safaricom", source="KMD_CAP", source_url="mock://", region_code=REGION,
            fetched_at=NOW - timedelta(days=3), valid_from=NOW - timedelta(days=3), valid_until=NOW - timedelta(days=2),
            stale=0, confidence=1.0, storm_flag=flag, flood_flag=0, planned_power=0, access_risk=0, payload_json="{}",
            derived_json=json.dumps({"kind": kind}, separators=(",", ":")), external_id=eid, created_at=NOW,
        ))
    seed.commit()
    report = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW)
    assert set(report["families"]) == {"storm", "cap", "flood"}
    storm = report["families"]["storm"]
    assert set(storm["regions"]) == {"NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"}  # every region, data or not
    assert storm["regions"][REGION]["precision"] == 0.75 and storm["overall"]["episodes_resolved"] == 12
    assert storm["regions"]["CST"]["verdict"] == VERDICT_INSUFFICIENT_DATA and storm["regions"]["CST"]["reason"]
    assert report["families"]["cap"]["regions"][REGION]["episodes"] == 1  # the alert, not the health row
    assert report["families"]["flood"]["overall"]["episodes"] == 0
    # The overall pass does not double-count incidents already attributed per region.
    assert storm["overall"]["incidents_warned"] == storm["regions"][REGION]["incidents_warned"] == 9


def test_score_region_rejects_an_unknown_family(seed):
    with pytest.raises(ValueError):
        score_region(seed.session, "safaricom", REGION, family="rumour", since=NOW - timedelta(days=1), now=NOW)


# ------------------------------------------------------------------------------ review findings


def test_f14_a_calm_reading_cuts_a_storm_claim_and_separates_two_flags(seed):
    """F14: a flag withdrawn after 15 minutes kept claiming six hours, absorbed a second flag
    raised at 05:00 into one episode, and credited an incident at 03:00 as warned."""
    start = NOW - timedelta(days=2)
    seed.weather(start, storm=True)
    for i in range(1, 20):  # 19 calm forecasts, 00:15 .. 04:45
        seed.weather(start + timedelta(minutes=15 * i), storm=False)
    seed.weather(start + timedelta(hours=5), storm=True)
    seed.commit()
    rows = backtest._family_rows(seed.session, "safaricom", family="storm", since=NOW - timedelta(days=30), until=NOW, now=NOW)
    episodes = build_episodes(rows, family="storm")
    assert [(e.start, e.end) for e in episodes] == [
        (start, start + timedelta(minutes=15)),                       # cut by the first calm reading
        (start + timedelta(hours=5), start + timedelta(hours=11)),    # its own 6 h claim
    ]
    assert not any(e.covers(start + timedelta(hours=3)) for e in episodes)


def test_f11_an_incident_just_after_until_inside_a_claim_is_a_hit_not_a_miss(seed):
    """F11: incidents were loaded only up to ``until``, so an episode raised two hours before a
    past ``until`` and followed by an incident an hour after it was scored a false alarm."""
    until = NOW - timedelta(days=5)
    seed.history()
    for i in range(11):
        start = until - timedelta(hours=2) - timedelta(days=2 * (10 - i))
        seed.storm(start, rows=1)
        seed.incident(start + timedelta(hours=3))
    seed.commit()
    score = score_region(seed.session, "safaricom", REGION, since=until - timedelta(days=30), until=until, now=NOW)
    assert (score.episodes_resolved, score.episodes_hit, score.precision) == (11, 11, 1.0)
    assert score.incidents == 10  # recall still counts only the window's incidents


def test_f19_history_is_measured_to_the_end_of_the_window_not_to_today(seed):
    """F19 (contested; adopted): a window that ended when the poller had run 40 days is
    judged on 40 days, however late it is scored."""
    until = NOW - timedelta(days=60)
    seed.weather(until - timedelta(days=40), storm=False)
    for i in range(12):
        start = until - timedelta(days=28) + timedelta(days=2 * i)
        seed.storm(start)
        seed.incident(start + timedelta(hours=1))
    seed.commit()
    score = score_region(seed.session, "safaricom", REGION, since=until - timedelta(days=30), until=until, now=NOW)
    assert score.history_days == 40.0 and score.verdict == VERDICT_INSUFFICIENT_DATA and score.precision is None
    # And a row stored after ``until`` is not history the window had.
    seed.weather(NOW - timedelta(days=1), storm=False, region="CST")
    seed.commit()
    later_only = score_region(seed.session, "safaricom", "CST", since=until - timedelta(days=30), until=until, now=NOW)
    assert later_only.history_days is None
    assert score_region(seed.session, "safaricom", "CST", since=NOW - timedelta(days=30), now=NOW).history_days == 1.0


def test_f13_lift_and_lead_are_withheld_whenever_precision_is(seed):
    """F13: one flag with one incident inside it computed to a 'lift' of 119, published beside
    INSUFFICIENT_DATA."""
    seed.history(days=10)
    seed.storm(NOW - timedelta(days=3))
    seed.incident(NOW - timedelta(days=3) + timedelta(hours=1))
    seed.incident(NOW - timedelta(days=6))
    seed.commit()
    score = precision_verdict_30d(seed.session, "safaricom", REGION, now=NOW)
    assert score["verdict"] == VERDICT_INSUFFICIENT_DATA
    assert score["precision"] is None and score["lift"] is None and score["median_lead_hours"] is None
    assert (score["episodes_hit"], score["incidents_warned"]) == (1, 1)  # the counts are still there


def test_f12_the_overall_lift_is_computed_in_region_hours():
    """F12: episodes from different regions were merged onto one wall clock while every region's
    incidents were counted, so an unflagged region's incidents halved the overall lift."""
    since, until = NOW - timedelta(hours=1440), NOW
    episodes = [Episode("AAA", since + timedelta(hours=100 * i), since + timedelta(hours=100 * i + 6)) for i in range(10)]
    incidents = []
    for i, e in enumerate(episodes):
        incidents.append((f"a{i}", f"A{i}", "AAA", e.start + timedelta(hours=1)))   # inside AAA's flag
        incidents.append((f"o{i}", f"O{i}", "AAA", e.start + timedelta(hours=50)))  # AAA, unflagged
        incidents.append((f"b{i}", f"B{i}", "BBB", e.start + timedelta(hours=2)))   # BBB, never flagged
    common = dict(family="storm", episodes=episodes, incidents=incidents, since=since, until=until, now=NOW,
                  history_start=NOW - timedelta(days=100), config=backtest.DEFAULT_BACKTEST)
    aaa = backtest._score(region_code="AAA", regions=["AAA"], **common)
    both = backtest._score(region_code="ALL", regions=["AAA", "BBB"], **common)
    assert aaa.lift == 23.0                        # (10/60) / (10/1380)
    assert both.lift == 23.5                       # (10/60) / (20/2820): BBB's hours count as BBB's
    assert both.flagged_hours == 60.0


def test_f05_the_overall_row_pools_only_regions_that_pass_the_floors_themselves(seed):
    """F05: the overall row took its history from the oldest row in ANY region, so twelve
    episodes from a 20-day-old region published a precision that region withheld."""
    seed.history(days=100, region="WNY")                 # old, but never flags
    seed.history(days=20, region="NBI_E")                # young, flags a lot
    _storms(seed, 12, hits=12, region="NBI_E")
    seed.commit()
    report = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW, families=("storm",))
    storm = report["families"]["storm"]
    assert storm["regions"]["NBI_E"]["verdict"] == VERDICT_INSUFFICIENT_DATA
    overall = storm["overall"]
    assert overall["verdict"] == VERDICT_INSUFFICIENT_DATA and overall["precision"] is None
    assert overall["contributing_regions"] == [] and "NBI_E" in overall["dropped_regions"]
    assert "pooling regions that each fail them" in overall["reason"]

    # With one region that stands on its own, the pool is that region only — and says so.
    _storms(seed, 12, hits=9, region="WNY")
    seed.commit()
    overall = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW,
                     families=("storm",))["families"]["storm"]["overall"]
    assert overall["contributing_regions"] == ["WNY"] and overall["precision"] == 0.75
    assert overall["episodes_resolved"] == 12 and "dropped" in overall["reason"]


def _w14_seed(seed: Seed) -> None:
    """The replayers' W14 case: two regions, both 100 days old. WNY never flags and has 30
    incidents; NBI_E flags twelve times, each followed by an incident."""
    seed.history(days=100, region="WNY")
    seed.history(days=100, region="NBI_E")
    for i in range(30):
        seed.incident(NOW - timedelta(days=29) + timedelta(hours=23 * i), region="WNY")
    _storms(seed, 12, hits=12, region="NBI_E")
    seed.commit()


def test_w14_recall_is_pooled_over_the_regions_that_pass_the_recall_floors(seed):
    """W14: the ALL row pooled recall over the PRECISION pool, which drops a region that never
    flags — exactly where the missed incidents are — so 12 warned of 12 read as recall 1.0.
    Over both regions it is 12 of 42."""
    _w14_seed(seed)
    report = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW,
                    regions=["WNY", "NBI_E"], families=("storm",))
    storm = report["families"]["storm"]
    assert storm["regions"]["WNY"]["verdict"] == VERDICT_INSUFFICIENT_DATA  # no episodes: out of the precision pool ...
    assert storm["regions"]["WNY"]["recall_verdict"] == VERDICT_MEASURED   # ... but squarely in the recall pool
    overall = storm["overall"]
    assert (overall["incidents"], overall["incidents_warned"], overall["recall"]) == (42, 12, 0.286)
    assert overall["recall_ci95"] == list(wilson_interval(12, 42)) and overall["recall_verdict"] == VERDICT_MEASURED
    assert overall["precision"] == 1.0 and overall["episodes_resolved"] == 12  # precision still NBI_E's alone (F05)
    assert overall["contributing_regions"] == ["NBI_E"] and list(overall["dropped_regions"]) == ["WNY"]
    assert overall["recall_contributing_regions"] == ["WNY", "NBI_E"] and overall["recall_dropped_regions"] == {}
    assert "precision pooled from NBI_E only" in overall["reason"] and "recall pooled from WNY, NBI_E" in overall["reason"]


def test_w14_a_region_short_of_the_recall_floor_leaves_only_the_recall_pool(seed):
    seed.history(days=100, region="WNY")
    for i in range(4):  # history, but only 4 incidents: out of the recall pool
        seed.incident(NOW - timedelta(days=20 - i), region="WNY")
    seed.history(days=100, region="NBI_E")
    _storms(seed, 12, hits=12, region="NBI_E")
    seed.commit()
    overall = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW,
                     regions=["WNY", "NBI_E"], families=("storm",))["families"]["storm"]["overall"]
    assert overall["recall_contributing_regions"] == ["NBI_E"] and overall["recall"] == 1.0
    assert "4 incident(s) in the window" in overall["recall_dropped_regions"]["WNY"]


def test_w14_the_cli_always_explains_the_all_row(seed, capsys):
    """The CLI printed notes only for rows that were not MEASURED, so a MEASURED ALL row gave no
    hint which regions its numbers came from."""
    _w14_seed(seed)
    report = replay(seed.session, "safaricom", since=NOW - timedelta(days=30), until=NOW, now=NOW,
                    regions=["WNY", "NBI_E"], families=("storm",))
    text = _script().format_report(report)
    all_notes = [line for line in text.splitlines() if line.startswith("  ALL: ")]
    assert len(all_notes) == 1 and "recall pooled from WNY, NBI_E" in all_notes[0]


def test_the_dashboard_publishes_the_measured_precision_beside_the_storm_flag(seed):
    """The services/dashboards.py wiring, end to end: None below the floor, the number above it."""
    seed.history()
    seed.weather(NOW - timedelta(minutes=5), storm=False, region="CST")  # a fresh reading elsewhere
    seed.commit()
    regions = {r["region_code"]: r for r in dashboards.regions_dashboard(seed.session, now=NOW)["regions"]}
    assert regions["WNY"]["signals"]["weather"]["precision_30d"] is None
    _storms(seed, 12, hits=9)
    seed.weather(NOW - timedelta(minutes=5), storm=False)
    seed.commit()
    regions = {r["region_code"]: r for r in dashboards.regions_dashboard(seed.session, now=NOW)["regions"]}
    assert regions["WNY"]["signals"]["weather"]["precision_30d"] == 0.75
    assert regions["CST"]["signals"]["weather"]["precision_30d"] is None


# ------------------------------------------------------------------------------ the dashboard hook


def test_the_hook_takes_exactly_what_the_dashboards_weather_block_has_in_scope():
    """services/dashboards.py is not this lane's file. The one-line change handed over is
    ``signal_precision_30d(session, operator_id, region_code, now=now)``; pin that every name
    it needs is a parameter of ``_weather_block`` as the file stands today."""
    in_scope = set(inspect.signature(dashboards._weather_block).parameters)
    assert {"session", "operator_id", "region_code", "now"} <= in_scope
    params = list(inspect.signature(signal_precision_30d).parameters)
    assert params[:3] == ["session", "operator_id", "region_code"] and "now" in params


def test_the_hook_never_raises_into_a_dashboard(seed, monkeypatch):
    monkeypatch.setattr(backtest, "score_region", lambda *a, **k: 1 / 0)
    assert signal_precision_30d(seed.session, "safaricom", REGION, now=NOW) is None


# ------------------------------------------------------------------------------ the script


def _script():
    spec = importlib.util.spec_from_file_location("backtest_signals_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_runs_on_an_empty_database_and_withholds_every_number(tmp_db, capsys):
    assert _script().main(["--since", "90d"]) == 0
    out = capsys.readouterr().out
    assert "Early-warning backtest  operator=safaricom" in out
    assert "== storm ==" in out and "== cap ==" in out and "== flood ==" in out
    assert "MEASURED " not in out and "INSUFFICIENT_DATA" in out
    assert "no stored signal history" in out


def test_the_script_json_on_a_seeded_database(seed, capsys):
    seed.history()
    _storms(seed, 12, hits=9)
    seed.commit()
    module = _script()
    # Absolute dates: the script reads the wall clock, and the seed is pinned to NOW.
    window = ["--since", "2026-08-19", "--until", "2026-09-18T12:00:00"]
    assert module.main([*window, "--family", "storm", "--region", "wny", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert list(report["families"]) == ["storm"] and list(report["families"]["storm"]["regions"]) == ["WNY"]
    assert report["families"]["storm"]["regions"]["WNY"]["episodes_hit"] == 9


@pytest.mark.parametrize("args", [["--since", "yesterday"], ["--since", "1d", "--until", "2d"]])
def test_the_script_rejects_a_bad_window(tmp_db, capsys, args):
    assert _script().main(args) == 2
    assert "error:" in capsys.readouterr().err


def test_parse_when_accepts_relative_and_absolute():
    module = _script()
    assert module.parse_when("90d", now=NOW) == NOW - timedelta(days=90)
    assert module.parse_when("12h", now=NOW) == NOW - timedelta(hours=12)
    assert module.parse_when("2026-06-01", now=NOW) == datetime(2026, 6, 1)
    assert module.parse_when("2026-06-01T09:00:00+03:00", now=NOW) == datetime(2026, 6, 1, 6, 0)
    assert module.parse_when(None, now=NOW) is None
