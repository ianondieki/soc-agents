"""Record the console against the real backend, for the static demo build (docs/DEMO_BUILD.md).

The public demo link is the frontend built in demo mode (``vite build --mode demo``): no server,
every API answer replayed from the file this script writes. Nothing here is mocked: the script
runs this repository's FastAPI app in-process on a throwaway SQLite file, drives it through its
own HTTP routes exactly as the console does, and writes down what each route answered.

What it records, in order:

``history``
    A believable recent past, each beat under a frozen clock (freezegun) so its timestamps are
    days or hours old: earlier outages at other sites, raised, decided, restored and closed;
    contracts, maintenance plans and windows, a scorecard, post-incident reviews, support cases
    and an eval run.
``s0``
    Every console read at the start of the demo: nothing open, the storm not yet launched.
``storm``
    The heavy-rain storm, one alarm at a time, as the Mission control button sends it. After
    each alarm: the ingest's answer, the agent run look-up the storm runner makes, the realtime
    frames the alarm published, and every console read again.
``final``
    Every read once the storm is over, plus every detail read (each ticket, review, scorecard,
    window and support case).
``actions``
    What one press does, for each press the demo offers: approve, reject or claim each card;
    a note, a restore, a close or a reassign on each open ticket; the shift handover; a few
    complaints. Each is made on its own copy of the final database, and stored as a patch
    (``diff``) against the reads before it, so the demo can apply presses in any order.
    Free text the visitor types replaces a placeholder (``SENTINELS``).

The operator profile is a real company's name, so the recording names a fictional operator
instead (``DEMO_OPERATOR_NAME``, default "Savanna Mobile"; set it empty to keep the profile's
own name for a private demo).

Usage: ``python scripts/demo/record.py OUT.json`` (the build script, scripts/demo/build.sh,
passes ``frontend/dist-demo/demo-data/recording.json``). Takes a few minutes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(tempfile.mkdtemp(prefix="noc-demo-"))
os.environ.update(
    DATABASE_URL=f"sqlite:///{(WORK / 'base.db').as_posix()}",
    OPERATOR_PROFILE="safaricom",
    LIVE_AGENT_DELAY_MS="0",
    NOC_SKIP_DOTENV="1",
    EMAIL_ENABLED="false",
    LLM_ENABLED="false",
    SCHEDULER_ENABLED="false",
    PIR_ENABLED="true",
    SCORECARDS_ENABLED="true",
    CONTRACTS_ENABLED="true",
    MAINTENANCE_ENABLED="true",
)
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from freezegun import freeze_time  # noqa: E402

import noc_agents.db.models as models  # noqa: E402
import noc_agents.main as main  # noqa: E402
from noc_agents.realtime.hub import hub  # noqa: E402

API = "/api/v1"
#: Placeholders for what the visitor types; the demo swaps in their words when it applies a patch.
SENTINELS = {"who": "⟦WHO⟧", "note": "⟦NOTE⟧", "reason": "⟦REASON⟧", "msp": "⟦MSP⟧"}
DEMO_PERSON = {"display_name": "Achieng Otieno", "role": "duty_manager"}

#: Every read that is not about one record. The console asks for exactly these URLs.
GLOBAL_READS = [
    "/health",
    f"{API}/profile",
    f"{API}/session",
    f"{API}/metrics/summary",
    f"{API}/incidents",
    f"{API}/hitl/pending",
    f"{API}/agents",
    f"{API}/audit?limit=1000",  # the demo answers ?limit=100 and 500 by slicing this one
    f"{API}/dashboard/regions",
    f"{API}/email/status",
    f"{API}/maintenance/tasks",
    f"{API}/maintenance/windows",
    f"{API}/maintenance/plans",
    f"{API}/metrics/productivity?window_hours=0",
    f"{API}/metrics/productivity?window_hours=24",
    f"{API}/notifications?window_hours=24&limit=60",
    f"{API}/pir",
    f"{API}/pir/awaiting-review",
    f"{API}/problems",
    f"{API}/public/status",
    f"{API}/runs",
    f"{API}/runs?graph_name=incident_lifecycle",
    f"{API}/scheduler/status",
    f"{API}/scorecards",
    f"{API}/vendors",
    f"{API}/shifts/ledger",
    f"{API}/shifts/current",
    f"{API}/signals/weather/regions",
    f"{API}/sites",
    f"{API}/contracts",
    f"{API}/contracts/status",
    f"{API}/demo/scenarios",
    f"{API}/demo/rain-storm/events",
    f"{API}/support/complaints?limit=200",
    f"{API}/support/complaints?limit=3",
    f"{API}/support/evals/latest",
    f"{API}/support/metrics",
    f"{API}/support/metrics?hours=0",
    f"{API}/support/kb",
    f"{API}/support/loop?hours=0",
    f"{API}/support/outages",
    f"{API}/support/surges",
]
#: Contract searches the drawer is likely to see; any other query gets the closest of these.
CONTRACT_QUERIES = ["restore", "penalty", "MTTR", "response time", "rain", "force majeure", "fibre cut", "credit"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Recorder:
    def __init__(self, client: TestClient) -> None:
        self.c = client
        self.bodies: list[Any] = []
        self._index: dict[str, int] = {}

    # ------------------------------------------------------------------ http
    def call(self, method: str, url: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        r = self.c.request(method, url, json=body) if body is not None else self.c.request(method, url)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.status_code != 204 and r.content else None

    def read(self, url: str) -> dict[str, Any]:
        """One GET as {status, body}: a 404 or 204 is recorded as such, never raised."""
        r = self.c.get(url)
        body: Any = None
        if r.content and r.status_code != 204:
            try:
                body = r.json()
            except ValueError:
                body = r.text
        return {"s": r.status_code, "b": body}

    def intern(self, answer: dict[str, Any]) -> list[Any]:
        """[status, body index]: identical bodies are stored once."""
        key = json.dumps(answer["b"], sort_keys=True, separators=(",", ":"))
        if key not in self._index:
            self._index[key] = len(self.bodies)
            self.bodies.append(answer["b"])
        return [answer["s"], self._index[key]]

    # ------------------------------------------------------------------ reads
    def incidents(self) -> list[dict[str, Any]]:
        rows = self.call("GET", f"{API}/incidents")
        return rows if isinstance(rows, list) else rows.get("items", [])

    def detail_reads(self) -> list[str]:
        """Every read about one record, for every record there is now."""
        urls: list[str] = []
        for inc in self.incidents():
            i = inc["id"]
            urls += [
                f"{API}/incidents/{i}",
                f"{API}/incidents/{i}/timeline",
                f"{API}/incidents/{i}/workflow",
                f"{API}/incidents/{i}/clock",
                f"{API}/incidents/{i}/regulatory",
                f"{API}/runs?incident_id={i}&graph_name=incident_lifecycle",
                f"{API}/briefs/{i}",
                f"{API}/support/incidents/{i}/customers",
                f"{API}/maintenance/incidents/{i}/stop-clock-proposal",
            ]
            if inc.get("site_id"):
                urls.append(f"{API}/memory/sites/{quote(inc['site_id'], safe='')}")
        for pir in self.call("GET", f"{API}/pir") or []:
            urls.append(f"{API}/pir/{pir['id']}")
        for card in self.call("GET", f"{API}/scorecards") or []:
            urls.append(f"{API}/scorecards/{quote(str(card['id']), safe='')}")
        for win in self.call("GET", f"{API}/maintenance/windows") or []:
            urls.append(f"{API}/maintenance/windows/{win['id']}")
        queue = self.call("GET", f"{API}/support/complaints?limit=200") or {}
        for case in queue.get("items", []) if isinstance(queue, dict) else queue:
            urls.append(f"{API}/support/complaints/{quote(str(case['id']), safe='')}")
        for q in CONTRACT_QUERIES:
            urls.append(f"{API}/contracts/clauses/search?q={quote(q)}")
        return list(dict.fromkeys(urls))

    def snapshot(self, urls: list[str]) -> dict[str, Any]:
        return {u: self.read(u) for u in urls}

    def frames_since(self, seq: int) -> list[dict[str, Any]]:
        return [dict(rec) for rec in list(hub._history) if getattr(rec, "seq", 0) > seq]


# ---------------------------------------------------------------------- the recent past


def history(rec: Recorder, now: datetime) -> None:
    """Earlier outages at sites the storm does not touch, plus the lanes' paperwork."""
    alarms = [
        # (hours ago, event, hours to restore). The first three fall in last month, for its scorecard.
        (
            35 * 24,
            {"site_id": "SFC-MTK-HUB-THK", "site_name": "Thika Mt Kenya HUB", "site_type": "HUB", "region_code": "MTK",
             "county": "Kiambu", "alarm_code": "FIBRE_CUT", "failure_domain": "TRANSMISSION", "technology": ["FIBRE", "4G"],
             "severity_raw": "MAJOR", "users_affected": 90000, "child_sites_down": 3,
             "description": "Fibre cut by road works on the Thika superhighway service lane"},
            6,
        ),
        (
            30 * 24,
            {"site_id": "SFC-WNY-HUB-KKG", "site_name": "Kakamega HUB", "site_type": "HUB", "region_code": "WNY",
             "county": "Kakamega", "alarm_code": "MAINS_FAIL", "failure_domain": "POWER", "technology": ["4G", "3G"],
             "severity_raw": "MAJOR", "users_affected": 70000, "battery_countdown_min": 120,
             "description": "KPLC outage across Kakamega town; site on batteries"},
            2,
        ),
        (
            24 * 24,
            {"site_id": "SFC-RFT-HUB-ELD", "site_name": "Eldoret Rift HUB", "site_type": "HUB", "region_code": "RFT",
             "county": "Uasin Gishu", "alarm_code": "MW_HOP_DOWN", "failure_domain": "TRANSMISSION", "technology": ["MW", "4G"],
             "severity_raw": "MAJOR", "users_affected": 110000, "child_sites_down": 4,
             "description": "Microwave hop to Eldoret down after a mast alignment shifted in high winds"},
            7,
        ),
        (
            74,
            {"site_id": "SFC-RFT-HUB-NKR", "site_name": "Nakuru Rift HUB", "site_type": "HUB", "region_code": "RFT",
             "county": "Nakuru", "alarm_code": "MAINS_FAIL", "failure_domain": "POWER", "technology": ["4G", "3G"],
             "severity_raw": "MAJOR", "users_affected": 120000, "battery_countdown_min": 180,
             "description": "KPLC mains lost at the Nakuru aggregation site; running on batteries"},
            3,
        ),
        (
            27,
            {"site_id": "SFC-CST-HUB-MSA", "site_name": "Mombasa Island HUB", "site_type": "HUB", "region_code": "CST",
             "county": "Mombasa", "alarm_code": "FIBRE_CUT", "failure_domain": "TRANSMISSION", "technology": ["FIBRE", "4G"],
             "severity_raw": "CRITICAL", "users_affected": 560000, "child_sites_down": 6,
             "access_notes": "Road works on the Mariakani stretch; county permit needed for night digging",
             "description": "Fibre cut on the Mombasa to Nairobi route near Mariakani; traffic on the backup ring"},
            5,
        ),
        (
            26,
            {"site_id": "SFC-CST-ENB-NYL12", "site_name": "Nyali eNodeB 12", "site_type": "ENODEB", "region_code": "CST",
             "county": "Mombasa", "alarm_code": "SITE_DOWN", "failure_domain": "TRANSMISSION", "technology": ["4G"],
             "severity_raw": "MAJOR", "users_affected": 14000, "parent_hub_id": "SFC-CST-HUB-MSA",
             "description": "Child site offline behind the Mombasa fibre cut"},
            4,
        ),
        (
            22,
            {"site_id": "SFC-NBIW-HUB-WLD", "site_name": "Westlands Aggregation HUB", "site_type": "HUB",
             "region_code": "NBI_W", "county": "Nairobi", "alarm_code": "MAINS_FAIL", "failure_domain": "POWER",
             "technology": ["4G", "5G"], "severity_raw": "MAJOR", "users_affected": 95000, "battery_countdown_min": 90,
             "description": "Mains failure in Westlands; generator started late"},
            2,
        ),
        (
            14 * 24,
            {"site_id": "SFC-WNY-HUB-KSM", "site_name": "Kisumu Western-Nyanza HUB", "site_type": "HUB",
             "region_code": "WNY", "county": "Kisumu", "alarm_code": "MAINS_FAIL", "failure_domain": "POWER",
             "technology": ["4G", "3G"], "severity_raw": "MAJOR", "users_affected": 150000, "battery_countdown_min": 120,
             "description": "KPLC mains lost in Kisumu; site on batteries"},
            2,
        ),
        (
            5 * 24,
            {"site_id": "SFC-WNY-HUB-KSM", "site_name": "Kisumu Western-Nyanza HUB", "site_type": "HUB",
             "region_code": "WNY", "county": "Kisumu", "alarm_code": "RECTIFIER_FAIL", "failure_domain": "POWER",
             "technology": ["4G", "3G"], "severity_raw": "MAJOR", "users_affected": 150000,
             "description": "Rectifier module failed at the Kisumu HUB; batteries draining"},
            4,
        ),
        (
            9,
            {"site_id": "SFC-WNY-HUB-KSM", "site_name": "Kisumu Western-Nyanza HUB", "site_type": "HUB",
             "region_code": "WNY", "county": "Kisumu", "alarm_code": "GENERATOR_FAIL", "failure_domain": "POWER",
             "technology": ["4G", "3G"], "severity_raw": "MAJOR", "users_affected": 160000, "battery_countdown_min": 45,
             "description": "Generator failed to start after a KPLC outage in Kisumu"},
            3,
        ),
    ]
    opened: list[tuple[datetime, str, float]] = []
    for hours_ago, event, fix_hours in alarms:
        at = now - timedelta(hours=hours_ago)
        with freeze_time(at, tick=True):
            res = rec.call("POST", f"{API}/events", event)
            inc = (res or {}).get("incident") or {}
            if inc.get("id") and inc["id"] not in [o[1] for o in opened]:
                opened.append((at, inc["id"], fix_hours))
        # Decide every card the alarm raised, a few minutes later, as the duty manager would.
        with freeze_time(at + timedelta(minutes=6), tick=True):
            for n, card in enumerate(rec.call("GET", f"{API}/hitl/pending") or []):
                if card.get("task_type") == "APPROVE_HANDOVER":
                    continue
                rec.call("POST", f"{API}/hitl/{card['id']}/claim", {"resolved_by": "Brian Kiprono"})
                if n == 1 and hours_ago == 27:
                    rec.call("POST", f"{API}/hitl/{card['id']}/reject",
                             {"resolved_by": "Brian Kiprono", "reason": "The customer notice repeats the HUB notice; one is enough"})
                else:
                    rec.call("POST", f"{API}/hitl/{card['id']}/approve", {"resolved_by": "Brian Kiprono"})

    for at, inc_id, fix_hours in opened:
        inc = rec.call("GET", f"{API}/incidents/{inc_id}")
        if inc.get("status") in ("RESTORED", "CLOSED", "CANCELLED"):
            continue
        with freeze_time(at + timedelta(minutes=40), tick=True):
            rec.call("POST", f"{API}/incidents/{inc_id}/notes", {
                "author": "Wanjiru Kamau", "author_role": "NOC", "source": "ui",
                "body": "Field engineer on site. Vendor confirms the fault and is working on it.",
                "msp_percent_complete": 40,
            })
        with freeze_time(at + timedelta(hours=fix_hours), tick=True):
            rec.call("POST", f"{API}/incidents/{inc_id}/notes", {
                "author": "Wanjiru Kamau", "author_role": "NOC", "source": "ui", "mark_restored": True,
                "body": "Service restored and traffic back to normal. Monitoring for an hour before closing.",
            })
            for card in rec.call("GET", f"{API}/hitl/pending") or []:
                if card.get("incident_id") == inc_id and card.get("task_type") != "APPROVE_HANDOVER":
                    rec.call("POST", f"{API}/hitl/{card['id']}/approve", {"resolved_by": "Brian Kiprono"})
        with freeze_time(at + timedelta(hours=fix_hours + 1.2), tick=True):
            rec.call("POST", f"{API}/incidents/{inc_id}/close", {
                "closed_by": "Wanjiru Kamau", "resolution_code": "CLOSED_NORMAL",
                "resolution_summary": "Restored and stable for an hour",
            }, ok=(200, 409))

    # The lanes' paperwork, a day ago and today.
    with freeze_time(now - timedelta(hours=20), tick=True):
        rec.call("POST", f"{API}/contracts/ingest-samples", {}, ok=(200, 201, 409))
        for body in (
            {"task_type": "GENERATOR_SERVICE", "standard_ref": "OEM-GEN-250H", "site_id": "SFC-WNY-HUB-KSM", "interval_hours": 250},
            {"task_type": "BATTERY_CAPACITY_TEST", "standard_ref": "IEEE-450", "site_class": "HUB", "interval_days": 90},
            {"task_type": "AIRCON_SERVICE", "standard_ref": "OEM-HVAC-Q", "site_id": "SFC-CST-HUB-MSA", "interval_days": 30},
        ):
            rec.call("POST", f"{API}/maintenance/plans", body, ok=(200, 201, 409, 422))

        def at(days: int, hour_eat: int) -> str:
            day = (now + timedelta(days=days)).astimezone(timezone(timedelta(hours=3)))
            return iso(day.replace(hour=hour_eat, minute=0, second=0, microsecond=0))

        windows = []
        for body in (
            {"scope": "SITE", "scope_ref": "SFC-WNY-HUB-KSM", "starts_at": at(1, 1), "ends_at": at(1, 4)},
            {"scope": "SITE", "scope_ref": "SFC-CST-HUB-MSA", "starts_at": at(3, 0), "ends_at": at(3, 3),
             "ca_approval_ref": "CA/NW/2026/114"},
            {"scope": "REGION", "scope_ref": "NBI_W", "starts_at": at(6, 1), "ends_at": at(6, 4)},
        ):
            win = rec.call("POST", f"{API}/maintenance/windows", body, ok=(200, 201, 422))
            if win and win.get("id"):
                windows.append(win["id"])
        for n, wid in enumerate(windows):
            rec.call("POST", f"{API}/maintenance/windows/{wid}/request-approval", {}, ok=(200, 201, 409, 422))
            if n < 2:  # two approved and scheduled, the third waits on the Approvals page
                for card in rec.call("GET", f"{API}/hitl/pending") or []:
                    if card.get("task_type") == "APPROVE_MAINTENANCE_WINDOW" and wid in json.dumps(card):
                        rec.call("POST", f"{API}/hitl/{card['id']}/approve", {"resolved_by": "Brian Kiprono"})
                rec.call("POST", f"{API}/maintenance/windows/{wid}/schedule", {}, ok=(200, 201, 403, 409, 422))
        rec.call("POST", f"{API}/support/demo/seed", {}, ok=(200, 201, 409))
        rec.call("POST", f"{API}/support/evals/run", {}, ok=(200, 201, 409))

    with freeze_time(now - timedelta(hours=3), tick=True):
        period = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")  # last month, which has ended
        rec.call("POST", f"{API}/scorecards/compute?period={period}", {}, ok=(200, 201, 409, 422))
        for inc in rec.incidents():
            if inc.get("priority") == "P1" and inc.get("status") == "CLOSED":
                rec.call("POST", f"{API}/incidents/{inc['id']}/pir", {}, ok=(200, 201, 409, 422))


# ---------------------------------------------------------------------- patches


def _canon(x: Any) -> Any:
    """What the browser reads back: JSON has one number type, so 5.0 arrives as 5."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_canon(v) for v in x]
    return x


#: The fields that name a list item, in the order tried (frontend/src/demo/patch.ts keyOf, same).
KEY_FIELDS = ("id", "task_id", "uid", "key", "code", "region_code", "incident_id", "name", "seq")


def _key(item: Any) -> str:
    if isinstance(item, dict):
        for k in KEY_FIELDS:
            v = _canon(item.get(k))
            if isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)):
                return f"{k}:{v}"
    return "j:" + json.dumps(_canon(item), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def diff(a: Any, b: Any) -> Any:
    """A patch that turns ``a`` into ``b``; ``None`` when they are equal. The demo applies it
    to whatever the read says at the time (frontend/src/demo/patch.ts), so a list patch names
    items by key, and a number patch is a delta (a count that one press moved by one moves by
    one again on top of another press)."""
    if a == b:
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        out: dict[str, Any] = {}
        for k, v in b.items():
            if k not in a:
                out[k] = {"$v": v}
            else:
                d = diff(a[k], v)
                if d is not None:
                    out[k] = d
        gone = [k for k in a if k not in b]
        return {"$o": out, **({"$d": gone} if gone else {})}
    if isinstance(a, list) and isinstance(b, list):
        ka, kb = [_key(x) for x in a], [_key(x) for x in b]
        if len(set(ka)) < len(ka) or len(set(kb)) < len(kb):
            return {"$v": b}
        old = dict(zip(ka, a))
        keep = set(kb)
        changed: dict[str, Any] = {}
        added: list[Any] = []
        for i, (k, x) in enumerate(zip(kb, b)):
            if k in old:
                d = diff(old[k], x)
                if d is not None:
                    changed[k] = d
            else:
                added.append([i, x])
        out_l: dict[str, Any] = {"rm": [k for k in ka if k not in keep], "ch": changed, "add": added}
        kept_before = [k for k in ka if k in keep]
        kept_after = [k for k in kb if k in old]
        if kept_before != kept_after:
            out_l["ord"] = kb
        return {"$l": out_l}
    numeric = (int, float)
    if isinstance(a, numeric) and isinstance(b, numeric) and not isinstance(a, bool) and not isinstance(b, bool):
        return {"$n": b - a}
    return {"$v": b}


def apply(value: Any, patch: Any) -> Any:
    """The reader of ``diff`` (frontend/src/demo/patch.ts is the same, line for line)."""
    if patch is None:
        return value
    if "$v" in patch:
        return patch["$v"]
    if "$n" in patch:
        return (value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0) + patch["$n"]
    if "$o" in patch:
        out = dict(value) if isinstance(value, dict) else {}
        for k, p in patch["$o"].items():
            out[k] = apply(out.get(k), p)
        for k in patch.get("$d", []):
            out.pop(k, None)
        return out
    lp = patch["$l"]
    items = list(value) if isinstance(value, list) else []
    gone = set(lp["rm"])
    items = [x for x in items if _key(x) not in gone]
    items = [apply(x, lp["ch"][_key(x)]) if _key(x) in lp["ch"] else x for x in items]
    for i, x in lp["add"]:
        items.insert(min(i, len(items)), x)
    if "ord" in lp:
        rank = {k: n for n, k in enumerate(lp["ord"])}
        items.sort(key=lambda x: rank.get(_key(x), len(rank)))
    return items


def delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """{url: {s, p}} for every read that changed, checked: applying it gives ``after`` exactly."""
    out = {}
    for u in set(before) | set(after):
        a = before.get(u, {"s": 404, "b": None})
        b = after.get(u, {"s": 404, "b": None})
        if a == b:
            continue
        p = diff(a["b"], b["b"]) if a["s"] == b["s"] else {"$v": b["b"]}
        if apply(a["b"], p) != b["b"]:
            p = {"$v": b["b"]}
        out[u] = {"s": b["s"], "p": p}
    return out


class Branches:
    """Copies of the database: every press is recorded on its own copy of the state before it."""

    def __init__(self) -> None:
        self._n = 0

    def save(self) -> Path:
        """Freeze the live database into a file a branch can start from."""
        models._engine.dispose()
        self._n += 1
        target = WORK / f"saved-{self._n}.db"
        live = Path(models._engine.url.database)
        shutil.copyfile(live, target)
        for ext in ("-wal", "-shm"):
            if Path(f"{live}{ext}").exists():
                shutil.copyfile(f"{live}{ext}", f"{target}{ext}")
        return target

    def start(self, saved: Path) -> None:
        models._engine.dispose()
        self._n += 1
        work = WORK / f"branch-{self._n}.db"
        shutil.copyfile(saved, work)
        for ext in ("-wal", "-shm"):
            if Path(f"{saved}{ext}").exists():
                shutil.copyfile(f"{saved}{ext}", f"{work}{ext}")
        models.init_db(f"sqlite:///{work.as_posix()}")


def record_actions(rec: Recorder, final: dict[str, Any]) -> dict[str, Any]:
    from noc_agents.support.ratelimit import complaint_limiter

    branches = Branches()
    who = SENTINELS["who"]
    base_db = branches.save()
    actions: dict[str, Any] = {}

    def press(name: str, start: Path, before: dict[str, Any], method: str, url: str, body: Any,
              then: Any = None) -> tuple[dict[str, Any], Path]:
        """One press on a fresh copy of ``start``; returns the reads after it and a saved copy."""
        branches.start(start)
        complaint_limiter._hits.clear()
        rec.call("POST", f"{API}/session", {"display_name": who, "role": DEMO_PERSON["role"]})
        seq = hub._seq
        at = datetime.now(timezone.utc)
        r = rec.c.request(method, url, json=body)
        answer: Any = None
        if r.content and r.status_code != 204:
            try:
                answer = r.json()
            except ValueError:
                answer = r.text
        after = rec.snapshot(GLOBAL_READS + rec.detail_reads())
        patches = delta(before, after)
        actions[name] = {
            "status": r.status_code,
            "response": answer,
            "patches": patches,
            "frames": rec.frames_since(seq),
            "at": iso(at),
        }
        if r.status_code >= 400:
            print(f"  {name}: {r.status_code} {str(answer)[:160]}", flush=True)
        return after, branches.save()

    cards = final[f"{API}/hitl/pending"]["b"] or []
    for card in cards:
        cid = card["id"]
        press(f"claim:{cid}", base_db, final, "POST", f"{API}/hitl/{cid}/claim", {"resolved_by": who})
        press(f"approve:{cid}", base_db, final, "POST", f"{API}/hitl/{cid}/approve",
              {"resolved_by": who, "reason": SENTINELS["reason"]})
        press(f"reject:{cid}", base_db, final, "POST", f"{API}/hitl/{cid}/reject",
              {"resolved_by": who, "reason": SENTINELS["reason"]})
    for inc in final[f"{API}/incidents"]["b"] or []:
        if inc.get("status") in ("RESTORED", "CLOSED", "CANCELLED"):
            continue
        i = inc["id"]
        note = {"author": who, "author_role": "NOC", "body": SENTINELS["note"], "source": "ui"}
        press(f"note:{i}", base_db, final, "POST", f"{API}/incidents/{i}/notes", note)
        restored, restored_db = press(f"restore:{i}", base_db, final, "POST", f"{API}/incidents/{i}/notes",
                                      {**note, "mark_restored": True})
        press(f"close:{i}", base_db, final, "POST", f"{API}/incidents/{i}/close",
              {"closed_by": who, "resolution_code": "CLOSED_NORMAL", "resolution_summary": f"Closed from the ticket page by {who}"})
        # The usual order on the floor: restore first, close after.
        press(f"close-after-restore:{i}", restored_db, restored, "POST", f"{API}/incidents/{i}/close",
              {"closed_by": who, "resolution_code": "CLOSED_NORMAL", "resolution_summary": f"Closed from the ticket page by {who}"})
        press(f"reassign:{i}", base_db, final, "POST", f"{API}/incidents/{i}/reassign",
              {"by": who, "reason": SENTINELS["reason"], "assignee_type": "MSP", "assignee_name": SENTINELS["msp"],
               "msp_name": SENTINELS["msp"]})
    press("monitor-tick", base_db, final, "POST", f"{API}/monitor/tick", {})

    handed, handed_db = press("handover", base_db, final, "POST", f"{API}/shifts/handover", None)
    for card in handed[f"{API}/hitl/pending"]["b"] or []:
        if card.get("task_type") == "APPROVE_HANDOVER" and card["id"] not in {c["id"] for c in cards}:
            cid = card["id"]
            press(f"claim:{cid}", handed_db, handed, "POST", f"{API}/hitl/{cid}/claim", {"resolved_by": who})
            press(f"approve:{cid}", handed_db, handed, "POST", f"{API}/hitl/{cid}/approve",
                  {"resolved_by": who, "reason": SENTINELS["reason"]})
            press(f"reject:{cid}", handed_db, handed, "POST", f"{API}/hitl/{cid}/reject",
                  {"resolved_by": who, "reason": SENTINELS["reason"]})

    for n, (text, _, _) in enumerate(COMPLAINTS):
        msisdn = COMPLAINT_MSISDN
        after, saved = press(f"complain:{n}", base_db, final, "POST", f"{API}/support/complaints",
                             {"body": text, "msisdn": msisdn, "channel": "web"})
        ref = ((actions[f"complain:{n}"]["response"] or {}).get("complaint") or {}).get("ref")
        if ref:
            branches.start(saved)
            r = rec.c.post(f"{API}/support/track", json={"ref": ref, "msisdn": msisdn})
            actions[f"track:{n}"] = {"status": r.status_code, "response": r.json() if r.content else None,
                                     "patches": {}, "frames": [], "at": iso(datetime.now(timezone.utc))}
            press(f"still-down:{n}", saved, after, "POST", f"{API}/support/track/still-down", {"ref": ref, "msisdn": msisdn})
    return actions


#: Complaints the demo can file: (text, what the visitor's words must mention to get this one, the
#: place it names). The demo picks the first whose words match (frontend/src/demo/server.ts), else
#: the last, and names the visitor's own place where the agents' reply names this one.
COMPLAINT_MSISDN = "0712345678"
COMPLAINTS = [
    ("No network in Nakuru since morning, calls are not going through", "nakuru|molo|naivasha|njoro|eldoret|rift", "Nakuru"),
    ("Hakuna network huku Kayole tangu asubuhi, siwezi kupiga simu",
     "kayole|embakasi|umoja|donholm|utawala|nairobi east|hakuna", "Kayole"),
    ("No signal in Thika town since the rain started", "thika|meru|nyeri|kiambu|mt kenya", "Thika"),
    ("I sent 2,000 shillings to the wrong number by mistake, please reverse the M-PESA transaction",
     "m-?pesa|reverse|wrong number|paybill|till", ""),
    ("My data bundle finished too fast, I bought 1GB yesterday and it is already gone", "bundle|data|internet|\\d\\s*[mg]b\\b", ""),
    ("Airtime was deducted for a call that did not go through", "airtime|deduct|charged|bill|credit", ""),
    ("My phone says no SIM card and I think someone swapped my line", "sim|swap|fraud|stolen|line", ""),
    ("No network at my place since morning", "", ""),
]


# ---------------------------------------------------------------------- the run


def main_record(out: Path) -> None:
    started = time.time()
    hub._history.clear()
    with TestClient(main.app) as client:
        rec = Recorder(client)
        rec.call("POST", f"{API}/session", DEMO_PERSON)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        history(rec, now)
        print(f"history done in {time.time() - started:.0f}s", flush=True)

        s0_at = datetime.now(timezone.utc)
        s0 = rec.snapshot(GLOBAL_READS + rec.detail_reads())

        templates = rec.call("GET", f"{API}/demo/rain-storm/events")["events"]
        storm_at = datetime.now(timezone.utc)
        steps = []
        for ev in templates:
            seq = hub._seq
            res = rec.call("POST", f"{API}/events", ev)
            inc = (res or {}).get("incident") or {}
            runs_url = f"{API}/runs?incident_id={inc.get('id')}&graph_name=incident_lifecycle"
            steps.append({
                "site_id": ev["site_id"],
                "response": res,
                "frames": rec.frames_since(seq),
                "reads": rec.snapshot(GLOBAL_READS + [runs_url]),
            })
        print(f"storm done in {time.time() - started:.0f}s", flush=True)
        final_at = datetime.now(timezone.utc)
        final = rec.snapshot(GLOBAL_READS + rec.detail_reads())
        actions = record_actions(rec, final)
        print(f"{len(actions)} presses done in {time.time() - started:.0f}s", flush=True)

        storm_doc = []
        last = dict(s0)
        for st in steps:
            now_reads = {**last, **st["reads"]}
            storm_doc.append({
                "site_id": st["site_id"],
                "response": st["response"],
                "frames": st["frames"],
                "reads": delta(last, now_reads),
            })
            last = now_reads
        doc = {
            "version": 1,
            "operator": "safaricom",
            "times": {"s0": iso(s0_at), "storm": iso(storm_at), "final": iso(final_at)},
            "s0": {u: rec.intern(a) for u, a in s0.items()},
            "storm": storm_doc,
            "final": delta(last, {**last, **final}),
            "actions": actions,
            "sentinels": SENTINELS,
            "complaints": {"msisdn": COMPLAINT_MSISDN, "texts": [{"text": t, "match": m, "place": pl} for t, m, pl in COMPLAINTS]},
            "bodies": rec.bodies,
        }
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    # A public page must never pass for a real operator's console or status page, so the public
    # build names a fictional operator. Replaced in the serialised text, so a list item's key
    # (which may be its own JSON) changes with the item.
    name = os.environ.get("DEMO_OPERATOR_NAME", "Savanna Mobile")
    if name:
        for real, fictional in (
            ("Safaricom PLC (demo profile)", f"{name} (fictional operator)"),
            ("Safaricom PLC", name),
            ("Safaricom", name),
        ):
            text = text.replace(real, fictional)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB) in {time.time() - started:.0f}s")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "frontend" / "dist-demo" / "demo-data" / "recording.json"
    try:
        main_record(target)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
