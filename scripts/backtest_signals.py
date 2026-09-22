"""Replay stored early-warning flags against the incidents that actually happened (spec §10.6).

Prints, per region and overall, for each flag family (``storm`` = the forecast poller,
``cap`` = KMD CAP warnings, ``flood`` = GloFAS river discharge):

* **precision** — of the flag episodes raised in the window whose claim has closed, how many
  were followed by an incident in that region while the flag was up;
* **recall** — of the incidents that started in the window, how many had a flag up at the
  moment they started;
* **lift** — the incident rate while flagged over the rate while not, which is the base rate
  a bare precision hides;
* **median lead** — hours from the flag first being stored to the incident (spec M6);
* the counts behind every number, and the verdict.

Below the data floor (90 days of stored history, per §10.6; 10 resolved episodes) a precision
is **not printed** — the verdict is ``INSUFFICIENT_DATA`` and the reason says which floor was
missed. The counts are still printed so it is visible how far off the floor is. All of the
arithmetic lives in ``services/backtest.py``; this file only parses arguments and formats.

Read-only: it never writes a row. §10.6 says the result is "written to
``signal_precision_30d``"; there is no column for that in the schema, and the Regions dashboard
instead calls ``services.backtest.signal_precision_30d`` on read — which also means the number
on the wallboard cannot be staler than the last incident.

Usage (Windows, from the repo root)::

    C:\\Python313\\python.exe scripts/backtest_signals.py                 # --since 90d, all families
    C:\\Python313\\python.exe scripts/backtest_signals.py --since 30d --family storm
    C:\\Python313\\python.exe scripts/backtest_signals.py --since 2026-06-01 --until 2026-09-01 --json
    C:\\Python313\\python.exe scripts/backtest_signals.py --operator airtel --region NBI

``--operator`` sets ``OPERATOR_PROFILE`` before settings are read; both operators share one
database file and every query here is scoped to the active profile's ``operator_id``.

Exit codes: 0 = report produced (whatever the verdicts); 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # runnable without an editable install
    sys.path.insert(0, str(ROOT / "src"))

_RELATIVE = re.compile(r"^\s*(\d+)\s*([dhw])\s*$", re.IGNORECASE)


def parse_when(value: str | None, *, now: datetime) -> datetime | None:
    """``90d`` / ``12h`` / ``2w`` (ago), or ``YYYY-MM-DD`` / an ISO timestamp, as naive UTC."""
    if not value:
        return None
    match = _RELATIVE.match(value)
    if match:
        n, unit = int(match.group(1)), match.group(2).lower()
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
        return now - delta
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        from datetime import timezone

        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _fmt_ratio(value: float | None, ci: list | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" + (f" [{ci[0]:.2f}-{ci[1]:.2f}]" if ci else "")


def format_report(report: dict) -> str:
    """The human table. One block per family; the overall row last; reasons underneath."""
    lines = [
        f"Early-warning backtest  operator={report['operator_id']}  window={report['since']} .. {report['until']}",
        f"floors: history >= {report['config']['min_history_days']} d, episodes >= {report['config']['min_episodes']}, "
        f"incidents >= {report['config']['min_incidents']} for recall; LOW CONFIDENCE below precision "
        f"{report['config']['min_precision']}",
        "",
    ]
    header = f"{'region':<7} {'verdict':<18} {'precision [95% CI]':<20} {'recall [95% CI]':<20} {'lift':>5} {'lead h':>7} {'episodes':>9} {'hit':>4} {'incidents':>9} {'warned':>6} {'history d':>9}"
    for family, block in report["families"].items():
        lines.append(f"== {family} ==")
        lines.append(header)
        rows = list(block["regions"].items()) + [("ALL", block["overall"])]
        notes = []
        for code, s in rows:
            episodes = f"{s['episodes_resolved']}+{s['episodes_pending']}" if s["episodes_pending"] else str(s["episodes_resolved"])
            lines.append(
                f"{code:<7} {s['verdict']:<18} {_fmt_ratio(s['precision'], s['precision_ci95']):<20} "
                f"{_fmt_ratio(s['recall'], s['recall_ci95']):<20} "
                f"{('-' if s['lift'] is None else format(s['lift'], '.2f')):>5} "
                f"{('-' if s['median_lead_hours'] is None else format(s['median_lead_hours'], '.1f')):>7} "
                f"{episodes:>9} {s['episodes_hit']:>4} {s['incidents']:>9} {s['incidents_warned']:>6} "
                f"{('-' if s['history_days'] is None else format(s['history_days'], '.0f')):>9}"
            )
            # The ALL row always gets its note: which regions each pooled number came from is the
            # thing a reader most needs, and a MEASURED verdict does not make it self-evident (W14).
            if s["reason"] and (code == "ALL" or s["verdict"] != "MEASURED"):
                notes.append(f"  {code}: {s['reason']}")
        lines.extend(notes)
        lines.append("")
    lines.append("episodes shown as resolved+pending; a pending episode's claim window has not closed and is not scored.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest stored storm/CAP/flood flags against incidents (spec §10.6)")
    parser.add_argument("--since", default="90d", help="window start: 90d / 12h / 2w ago, or YYYY-MM-DD (default 90d)")
    parser.add_argument("--until", default=None, help="window end (default now)")
    parser.add_argument("--operator", help="OPERATOR_PROFILE to report on (default: the current one)")
    parser.add_argument("--region", action="append", help="limit to this region code (repeatable)")
    parser.add_argument(
        "--family", action="append", choices=("storm", "cap", "flood"),
        help="limit to this flag family (repeatable; default all three)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of a table")
    args = parser.parse_args(argv)

    if args.operator:
        # Before the first settings read: the operator clause comes from the active profile.
        os.environ["OPERATOR_PROFILE"] = args.operator

    from noc_agents.config import clear_settings_cache, get_settings
    from noc_agents.db.models import get_session, init_db, utcnow
    from noc_agents.services.backtest import backtest_config, replay

    now = utcnow()
    try:
        since = parse_when(args.since, now=now)
        until = parse_when(args.until, now=now) or now
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if since is None or since >= until:
        print("error: --since must be before --until", file=sys.stderr)
        return 2

    clear_settings_cache()
    settings = get_settings()
    init_db(settings.database_url)
    session = get_session()
    try:
        report = replay(
            session,
            settings.operator.operator_id,
            since=since,
            until=until,
            now=now,
            regions=[r.upper() for r in args.region] if args.region else None,
            families=tuple(args.family) if args.family else ("storm", "cap", "flood"),
            config=backtest_config(settings.operator),
        )
    finally:
        session.close()
        clear_settings_cache()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
