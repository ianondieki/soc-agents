"""Transfer size and caching for a first load on a weak link (round 8 performance pass).

Before this, the 453 KB script and every JSON list crossed the wire uncompressed, and the
hashed bundle carried no cache header, so a manager on Slow 4G waited for the same bytes on
every visit. These pin the three properties that fixed it:

* an HTTP answer of 1 KiB or more goes out gzipped to a client that accepts gzip, a small one
  and a client that does not ask are left alone, and the SSE feed is never buffered by it;
* the WebSocket feed still connects through the middleware stack (it is HTTP-only);
* files under ``/assets`` (Vite names them after a hash of their bytes) are cacheable for a
  year as ``immutable``, while ``index.html``, which names the current hashes, is always
  revalidated, and the unhashed files at the root of the build (the self-hosted fonts) are
  kept for a week.
"""

from __future__ import annotations

import importlib
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.realtime.hub import hub

REAL_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
IMMUTABLE = "public, max-age=31536000, immutable"
HASHED_NAME = re.compile(r"-[A-Za-z0-9_-]{8}\.[a-z0-9]+$")

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


def _reload_main():
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    auth.reset_sessions()
    hub._history.clear()
    return importlib.reload(main)


@pytest.fixture()
def app_with(tmp_path, monkeypatch):
    """Reload ``noc_agents.main`` on its own SQLite file, optionally serving ``dist``."""

    def build(dist: Path | None = None):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'gzip.db').as_posix()}")
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        if dist is not None:
            monkeypatch.setenv("NOC_FRONTEND_DIST", str(dist))
        return _reload_main()

    yield build
    monkeypatch.delenv("NOC_FRONTEND_DIST", raising=False)
    _reload_main()  # back to the default shape (and the real build) for the rest of the session


def _fake_dist(root: Path) -> Path:
    """A build the shape Vite writes: index.html, hashed files under assets/, public/ files."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "fonts").mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Kenya NOC</title><div id=root></div>")
    (dist / "assets" / "index-AbC123xy.js").write_text("export const rows = [" + "'INC000001'," * 400 + "];\n")
    (dist / "fonts" / "ibm-plex-sans-latin-400-normal.woff2").write_bytes(b"wOF2" + bytes(range(256)) * 16)
    return dist


def test_a_large_json_answer_is_gzipped_and_a_small_one_is_not(app_with):
    main = app_with()
    with TestClient(main.app) as client:
        assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 200

        plain = client.get("/api/v1/runs", headers={"Accept-Encoding": "identity"})
        assert plain.status_code == 200
        assert "content-encoding" not in plain.headers
        assert len(plain.content) > 1024, "the run list should be big enough to be worth compressing"

        packed = client.get("/api/v1/runs", headers={"Accept-Encoding": "gzip"})
        assert packed.status_code == 200
        assert packed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in packed.headers.get("vary", "").lower()
        assert packed.json() == plain.json()  # the client decodes it to the same answer
        assert int(packed.headers["content-length"]) < len(plain.content) // 2

        small = client.get("/health", headers={"Accept-Encoding": "gzip"})
        assert small.status_code == 200
        assert "content-encoding" not in small.headers  # under 1 KiB: not worth a gzip frame


def test_the_middleware_is_http_only_and_leaves_the_sse_feed_unbuffered(app_with):
    from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

    main = app_with()
    gz = [m for m in main.app.user_middleware if m.cls is GZipMiddleware]
    assert len(gz) == 1
    assert gz[0].kwargs.get("minimum_size") == 1024
    # The SSE feed (/api/v1/stream/events) must flush frame by frame; Starlette's default
    # exclusion list is what keeps text/event-stream out of the compressor, so it must not be
    # replaced by a list that drops it.
    assert "exclude_content_types" not in gz[0].kwargs
    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES
    assert "font/woff2" in DEFAULT_EXCLUDED_CONTENT_TYPES  # already compressed


def test_the_websocket_still_connects_through_the_middleware(app_with):
    main = app_with()
    with TestClient(main.app) as client:
        assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 200
        with client.websocket_connect("/ws/ops", headers={"Accept-Encoding": "gzip"}) as ws:
            box: list[dict] = []
            reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
            reader.start()
            reader.join(10)
            assert box, "no frame replayed over /ws/ops within 10 s"
            assert {"type", "payload", "ts"} <= set(box[0])


def test_hashed_assets_are_immutable_the_shell_revalidates_and_fonts_keep_a_week(app_with, tmp_path):
    main = app_with(_fake_dist(tmp_path))
    with TestClient(main.app) as client:
        asset = client.get("/assets/index-AbC123xy.js", headers={"Accept-Encoding": "gzip"})
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == IMMUTABLE
        assert asset.headers["content-encoding"] == "gzip"
        assert asset.text.startswith("export const rows")

        # A revalidation that hits answers 304 and still says immutable.
        again = client.get("/assets/index-AbC123xy.js", headers={"If-None-Match": asset.headers["etag"]})
        assert again.status_code == 304
        assert again.headers["cache-control"] == IMMUTABLE

        assert client.get("/assets/missing-00000000.js").status_code == 404

        for path in ("/", "/showcase", "/index.html"):
            shell = client.get(path)
            assert shell.status_code == 200
            assert shell.headers["cache-control"] == "no-cache", path
            assert "Kenya NOC" in shell.text

        font = client.get("/fonts/ibm-plex-sans-latin-400-normal.woff2", headers={"Accept-Encoding": "gzip"})
        assert font.status_code == 200
        assert font.headers["cache-control"] == "public, max-age=604800"
        assert "content-encoding" not in font.headers  # woff2 is compressed already
        assert font.content.startswith(b"wOF2")


@pytest.mark.skipif(not (REAL_DIST / "assets").exists(), reason="frontend not built: no frontend/dist/assets")
def test_the_real_build_names_every_asset_by_hash_and_serves_it_immutable(app_with):
    files = sorted(p for p in (REAL_DIST / "assets").iterdir() if p.is_file())
    assert files
    # "immutable" is only true if the name changes whenever the bytes do.
    unhashed = [p.name for p in files if not HASHED_NAME.search(p.name)]
    assert not unhashed, f"files under assets/ without a content hash in the name: {unhashed}"
    main = app_with(REAL_DIST)
    with TestClient(main.app) as client:
        script = next((p for p in files if p.suffix == ".js"), files[0])
        r = client.get(f"/assets/{script.name}", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["cache-control"] == IMMUTABLE
        if script.stat().st_size >= 1024:
            assert r.headers["content-encoding"] == "gzip"
        assert client.get("/").headers["cache-control"] == "no-cache"

