from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

os.environ.setdefault("OPERATOR_PROFILE", "safaricom")

from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import get_session, init_db
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import drain_once
from noc_agents.services.handover import build_handover


def main() -> None:
    demo_db = os.path.join(ROOT, "data", "demo_safaricom.db")
    os.makedirs(os.path.dirname(demo_db), exist_ok=True)
    if os.path.exists(demo_db):
        os.remove(demo_db)
    os.environ["DATABASE_URL"] = f"sqlite:///{demo_db.replace(os.sep, '/')}"

    clear_settings_cache()
    settings = get_settings("safaricom")
    init_db(os.environ["DATABASE_URL"])
    session = get_session()
    op = settings.operator
    print("=== Safaricom NOC multi-agent demo (floor-aligned) ===")
    print(f"Operator: {op.display_name}")
    print(f"Network: ~{op.network_stats.get('approx_sites')} sites / "
          f"{op.network_stats.get('approx_subscribers')} subs / "
          f"{op.network_stats.get('geographical_regions')} regions")
    print(f"Autonomy: {op.autonomy_level}")
    print(f"Priorities: P4<{op.priority_thresholds.P4_max_users+1}, "
          f"P3<{op.priority_thresholds.P3_max_users+1}, "
          f"P2<{op.priority_thresholds.P2_max_users+1}, else P1")
    print()

    events = [
        EventIngest(
            site_id="SFC-NBIE-HUB-EMB",
            site_name="Embakasi East Aggregation HUB",
            site_type="HUB",
            region_code="NBI_E",
            county="Nairobi",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=450000,
            access_notes="Genset not auto-started",
            description="Nairobi East HUB power — expect Egypro",
        ),
        EventIngest(
            site_id="SFC-NBIW-HUB-WLD",
            site_name="Westlands Aggregation HUB",
            site_type="HUB",
            region_code="NBI_W",
            county="Nairobi",
            alarm_code="RADIO_CELL_DOWN",
            failure_domain="RADIO",
            users_affected=120000,
            description="Nairobi West Huawei radio — expect Huawei Radio",
        ),
        EventIngest(
            site_id="SFC-MTK-HUB-THK",
            site_name="Thika Mt Kenya HUB",
            site_type="HUB",
            region_code="MTK",
            county="Kiambu",
            alarm_code="TX_FIBRE_CUT",
            failure_domain="TRANSMISSION",
            users_affected=160000,
            description="Mt Kenya TX — expect Soliton/Egypro Fibre",
        ),
        EventIngest(
            site_id="SFC-CST-HUB-MSA",
            site_name="Mombasa Island HUB",
            site_type="HUB",
            region_code="CST",
            county="Mombasa",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=220000,
            description="Coast power — ATC/Camusat passive pool",
        ),
        EventIngest(
            site_id="SFC-RFT-HUB-NKR",
            site_name="Nakuru Rift HUB",
            site_type="HUB",
            region_code="RFT",
            county="Nakuru",
            alarm_code="GENSET_FAIL",
            failure_domain="POWER",
            users_affected=180000,
            description="Rift power — Tetranet",
        ),
        EventIngest(
            site_id="SFC-WNY-HUB-KSM",
            site_name="Kisumu Western-Nyanza HUB",
            site_type="HUB",
            region_code="WNY",
            county="Kisumu",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=190000,
            description="Western-Nyanza power — Tetranet",
        ),
        EventIngest(
            site_id="SFC-MTK-BTS-MRI08",
            site_name="Meru Rural BTS 08",
            site_type="BTS",
            region_code="MTK",
            county="Meru",
            alarm_code="SITE_DOWN",
            failure_domain="POWER",
            users_affected=3200,
            description="Small site — P4 class users",
        ),
    ]

    for e in events:
        inc = process_event(session, settings, e)
        # Transmit what the run queued (spec §7.0.2). A no-op when OUTBOX_SYNC_DRAIN already
        # drained inside process_event; the real work when it is off.
        drain_once(session)
        print(
            f"  {inc.incident_number} | {inc.priority} | {inc.site_id} | {inc.region_code} | "
            f"MSP={inc.responsible_msp or inc.msp_name} | FE={inc.fe_name} | "
            f"OEM={inc.radio_oem} | esc={inc.escalated_at} | HITL={inc.hitl_state}"
        )
        assert len(inc.incident_number) == 9 and inc.incident_number.startswith("INC")

    ho = build_handover(session, settings.operator)
    print()
    print("--- Handover preview ---")
    print(ho["subject"])
    print(f"Watchlist items: {ho['watch_count']}")
    print()
    print("Demo complete. Start API: uvicorn noc_agents.main:app --app-dir src --port 8000")
    session.close()


if __name__ == "__main__":
    main()
