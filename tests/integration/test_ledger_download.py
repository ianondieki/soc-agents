"""GET /api/v1/shifts/ledger/{shift_id}.xlsx — built in memory, streamed, never a path.

Spec §5.3.9 (P2) and §7.9.3. Two things are under test and the second one is the point:

1. the download works — xlsx media type, ``Content-Disposition``, bytes that openpyxl
   opens, the shift's ledger rows inside, and **no file written to disk by the route**
   (defect #6: the on-disk workbook left the request path entirely);
2. ``shift_id`` comes out of the URL, so it is attacker input. Every traversal shape is
   rejected and none of them is ever resolved to a file.

On (2), note what the *client* does before the server sees anything: httpx (and every
browser, and every proxy) removes ``.`` and ``..`` segments from a path per RFC 3986 §5.2.4
while resolving it against the base URL. A literal ``../../etc/passwd`` in the URL is
therefore normalised away in transit and the request arrives somewhere else entirely — it
never reaches this route. What CAN arrive here is a percent-encoded separator, which is
decoded into ``scope["path"]`` after routing decisions are made, plus every non-separator
shape (Windows drive letters, UNC, NUL). Those are asserted as 422 individually; the
normalised ones are asserted with the weaker but still true property that no workbook ever
comes back. Both sets are listed below so the next reader knows which is which.
"""

from __future__ import annotations

import importlib
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from noc_agents.realtime.hub import hub

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}

#: Traversal shapes that survive the client's RFC 3986 normalisation and reach the route.
#: Every one of these must be 422 — rejected as a string, before a Path is built.
REJECTED_AT_THE_ROUTE = [
    "..%2F..%2Fetc%2Fpasswd",  # encoded separators: decoded only after routing
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",  # the dots encoded too
    "....//....//etc/passwd",  # the "..././" filter-bypass shape
    r"..\..\windows\win.ini",  # backslash traversal (Windows separators survive httpx)
    r"C:\Windows\win.ini",  # absolute path, drive letter
    "/etc/passwd",  # absolute POSIX path
    "//etc/passwd",  # protocol-relative / double slash
    r"\\\\fileserver\share\ledger",  # UNC
    "2026-09-17_DAY%00",  # NUL truncation
    "2026-09-17_EVENING",  # a shift that does not exist
    "2026-9-17_DAY",  # unpadded date: the pattern is exact
    "safaricom:2026-09-17:DAY",  # the DB key itself — the URL never carries the operator
    "",  # empty
]

#: Traversal shapes the client normalises away before the request is sent. They cannot be
#: asserted as 422 (the server never sees them), only as "no workbook ever comes back".
NORMALISED_BY_THE_CLIENT = [
    "../2026-09-17_DAY",
    "../../../../etc/passwd",
    "2026-09-17_DAY/../../evil",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "ledger_download.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()


def _ledger_files() -> list[str]:
    """Everything under LEDGER_DIR right now — the route must not add to this."""
    from noc_agents.services.ledger import ledger_root

    root = ledger_root()
    if not root.exists():
        return []
    return sorted(p.as_posix() for p in root.rglob("*"))


def _url_shift_id(client: TestClient) -> str:
    """The current shift as §7.9.3 spells it in the URL: ``2026-09-17_DAY``.

    The stored key (``shift_ledger.shift_id``) is ``<operator>:<date>:<SHIFT>``; the URL
    form carries no operator, because the route is operator-scoped already.
    """
    _, date, shift = client.get("/api/v1/shifts/current").json()["shift_id"].split(":")
    return f"{date}_{shift}"


def test_download_returns_an_xlsx_built_in_memory(client):
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    sid = _url_shift_id(client)
    before = _ledger_files()

    r = client.get(f"/api/v1/shifts/ledger/{sid}.xlsx")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == XLSX_MEDIA_TYPE
    assert r.headers["content-disposition"] == f'attachment; filename="ledger_{sid}.xlsx"'
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:2] == b"PK"  # a real xlsx is a zip

    wb = load_workbook(BytesIO(r.content))
    assert wb.sheetnames == ["ShiftLedger", "Handover"]
    ws = wb["ShiftLedger"]
    assert ws.cell(row=1, column=1).value == "Row written (UTC)"
    numbers = [row[1] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert numbers == [inc["incident_number"]]
    assert wb["Handover"].cell(row=1, column=1).value == f"Ledger shift: {sid}"

    # Defect #6: the request path no longer touches the file system at all.
    assert _ledger_files() == before


def test_a_shift_with_no_rows_is_an_empty_workbook_not_a_404(client):
    """"Nothing happened on nights" is exactly what a supervisor downloads to prove."""
    client.post("/api/v1/events", json=HUB_EVENT)
    other = "2019-01-01_NIGHT"

    r = client.get(f"/api/v1/shifts/ledger/{other}.xlsx")

    assert r.status_code == 200
    ws = load_workbook(BytesIO(r.content))["ShiftLedger"]
    assert ws.max_row == 1  # headers only


@pytest.mark.parametrize("shift_id", REJECTED_AT_THE_ROUTE)
def test_traversal_shaped_shift_ids_are_422(client, shift_id):
    client.post("/api/v1/events", json=HUB_EVENT)
    before = _ledger_files()

    r = client.get(f"/api/v1/shifts/ledger/{shift_id}.xlsx")

    assert r.status_code == 422, f"{shift_id!r} -> {r.status_code}"
    assert r.headers["content-type"].startswith("application/json")
    assert _ledger_files() == before  # nothing was resolved, opened or created


@pytest.mark.parametrize("shift_id", NORMALISED_BY_THE_CLIENT)
def test_client_normalised_traversals_never_yield_a_workbook(client, shift_id):
    client.post("/api/v1/events", json=HUB_EVENT)
    before = _ledger_files()

    r = client.get(f"/api/v1/shifts/ledger/{shift_id}.xlsx")

    assert r.headers.get("content-type", "") != XLSX_MEDIA_TYPE
    assert r.content[:2] != b"PK"
    assert _ledger_files() == before


def test_the_guard_is_the_resolved_path_not_a_string_prefix():
    """``_validated_shift_id`` answers with ``Path.is_relative_to`` against the ledger root.

    Called directly, because the §7.9.3 pattern rejects every traversal shape before the
    path check can fire in a request. The check is not decoration: it is what still holds
    if the pattern is ever loosened, and a string ``startswith`` would not hold — a sibling
    directory named ``<root>-backup`` has the root as a string prefix and is outside it.
    """
    import noc_agents.main as main
    from noc_agents.services.ledger import ledger_root

    root = ledger_root().resolve()
    assert main._validated_shift_id("2026-09-17_DAY") == "2026-09-17_DAY"
    assert (root / "ledger_2026-09-17_DAY.xlsx").resolve().is_relative_to(root)

    sibling = Path(f"{root}-backup") / "ledger.xlsx"
    assert str(sibling).startswith(str(root))  # the old check would have passed this
    assert not sibling.is_relative_to(root)  # the new one does not


def test_url_shift_id_maps_to_the_stored_key_and_never_to_a_path():
    import noc_agents.main as main

    assert main._stored_shift_key("safaricom", "2026-09-17_DAY") == "safaricom:2026-09-17:DAY"
    assert main._stored_shift_key("airtel", "2026-09-17_NIGHT") == "airtel:2026-09-17:NIGHT"
