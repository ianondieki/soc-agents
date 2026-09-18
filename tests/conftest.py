from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Hard-set BEFORE any noc_agents import: config.py loads .env at import time with
# override=False, and dotenv only fills keys that are ABSENT, so an explicit value
# (even "") wins over both .env and a value exported by the calling shell.
# NOC_SKIP_DOTENV=1 additionally makes config._load_dotenv() return immediately, so
# a developer's local .env cannot reach the suite at all — belt and braces, because
# the override=False trick only protects the keys listed below.
os.environ["NOC_SKIP_DOTENV"] = "1"
os.environ["OPERATOR_PROFILE"] = "safaricom"
os.environ["AUTONOMY_LEVEL"] = "L2_GUARDED"  # config.py reads it ahead of both YAML sources
os.environ["LIVE_AGENT_DELAY_MS"] = "0"
os.environ["EMAIL_ENABLED"] = "false"
os.environ["LLM_ENABLED"] = "false"
# SCHEDULER_ENABLED was NOT pinned here, and that is a live test-pollution bug: ten test
# files construct `TestClient(main.app)`, which runs the lifespan, so a developer with
# SCHEDULER_ENABLED=true exported in their shell gets a real Scheduler with a ticking
# background task inside the suite — acquiring a DB lease and draining the outbox while
# tests assert on those very rows. Confirmed reproducible before pinning it.
os.environ["SCHEDULER_ENABLED"] = "false"
# Deliberately NOT pinning OUTBOX_DISPATCH_ENABLED here. SCHEDULER_ENABLED=false already
# means no ticker starts, so no job runs; pinning the per-job flag as well would stop
# test_scheduler_lease.py from exercising the outbox job when it enables the scheduler
# itself, which is real behaviour worth testing.
# Phase 4 feature lanes. Every one of these defaults to false in code, so pinning them
# changes nothing about what the suite tests -- it stops a developer who has exported one
# in their shell from testing a DIFFERENT system than CI does. That is not hypothetical:
# with HOUSEKEEPING_ENABLED and HOUSEKEEPING_APPLY both exported, the housekeeping job is
# the one piece of this codebase that DELETES rows, and the suite runs against real
# database files. Tests that need a lane on turn it on themselves with monkeypatch.
os.environ["SCORECARDS_ENABLED"] = "false"
os.environ["PIR_ENABLED"] = "false"
os.environ["REGULATORY_ENABLED"] = "false"
os.environ["HOUSEKEEPING_ENABLED"] = "false"
os.environ["HOUSEKEEPING_APPLY"] = "false"
os.environ["MEMORY_ENABLED"] = "false"
for _key in (
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "DEMO_EMAIL_TO",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
):
    os.environ[_key] = ""  # "" blocks the dotenv refill and reads as "not configured"


@pytest.fixture(scope="session", autouse=True)
def isolated_ledger_dir(tmp_path_factory):
    """Excel shift ledgers go to a temp dir; ledger_root() reads LEDGER_DIR at call time."""
    folder = tmp_path_factory.mktemp("shift_ledgers")
    os.environ["LEDGER_DIR"] = str(folder)
    yield folder


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    from noc_agents.config import clear_settings_cache, get_settings
    from noc_agents.db.models import init_db, get_session

    clear_settings_cache()
    settings = get_settings()
    # force test db
    settings = settings.model_copy(update={"database_url": url})
    init_db(url)
    session = get_session()
    yield settings, session
    session.close()
    clear_settings_cache()
