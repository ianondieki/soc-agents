"""``GET /`` without a built frontend: a page that says what is missing, not a bare 404.

The symptom this guards against was seen on a presenter's machine: a port that answers JSON at
``/`` looks like a broken server, and the time goes into the wrong investigation. With no
``frontend/dist`` the API now answers ``/`` with a short HTML page (503: the thing asked for
is not ready) naming the build command and the dev-server alternative, while every API route
and ``/health`` keep working exactly as before. ``NOC_FRONTEND_DIST`` is how a test (or a
deployment that builds elsewhere) points the app at a different folder.
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.realtime.hub import hub


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


def test_without_a_build_the_root_explains_itself_and_the_api_still_answers(tmp_path, monkeypatch):
    empty = tmp_path / "no-dist"  # does not exist: exactly a checkout before `npm run build`
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'ui.db').as_posix()}")
    monkeypatch.setenv("NOC_FRONTEND_DIST", str(empty))
    main = _reload_main()
    try:
        assert main.FRONTEND_DIST == empty and not main.FRONTEND_DIST.exists()
        with TestClient(main.app) as client:
            root = client.get("/")
            assert root.status_code == 503
            assert root.headers["content-type"].startswith("text/html")
            assert "npm run build" in root.text and "npm run dev" in root.text
            assert "<script" not in root.text  # static instructions, nothing executable
            assert client.get("/health").status_code == 200
            assert client.get("/api/v1/profile").status_code == 200
            # No catch-all without a build: a deep link is an honest 404, not the page.
            assert client.get("/showcase").status_code == 404
            assert "/" not in {getattr(r, "path", None) for r in main.app.routes if getattr(r, "name", "") == "spa_fallback"}
    finally:
        monkeypatch.delenv("NOC_FRONTEND_DIST", raising=False)
        _reload_main()  # back to the real build for the rest of the session
