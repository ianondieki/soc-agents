"""Screenshot every UI route at phone and desktop widths against a running stack.

    python scripts/screenshots.py docs/screenshots [http://127.0.0.1:8000]

Seeds the rain storm first when the board is empty, so every page has data. Needs the
``playwright`` package (``pip install playwright``) and a Chromium it can find; the
browser is launched headless. Console errors and warnings per page are printed at the
end — the Google Fonts request failing offline is the one expected line.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
os.makedirs(OUT, exist_ok=True)

ROUTES = [
    "/", "/showcase", "/incidents", "/hitl", "/shift", "/agents", "/workflow", "/problems", "/regions",
    "/maintenance", "/audit", "/contracts", "/pirs", "/scorecards", "/wallboard", "/settings",
]
VIEWPORTS = {"1440": (1440, 900), "375": (375, 812)}


def _get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.load(r)


incidents = _get("/api/v1/incidents")
if not incidents:
    req = urllib.request.Request(f"{BASE}/api/v1/demo/rain-storm", method="POST")
    with urllib.request.urlopen(req) as r:
        r.read()
    incidents = _get("/api/v1/incidents")
if incidents:
    ROUTES.insert(3, f"/incidents/{incidents[0]['id']}")

problems: dict[str, list[str]] = {}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for vp, (w, h) in VIEWPORTS.items():
        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
        page.add_init_script("try{sessionStorage.setItem('noc_auto_storm_v1','1')}catch(e){}")
        for route in ROUTES:
            logs.clear()
            name = "incident_detail" if route.startswith("/incidents/") else (route.strip("/").replace("/", "_") or "home")
            try:
                page.goto(f"{BASE}{route}", wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(1000)
                page.screenshot(path=f"{OUT}/{name}_{vp}.png", full_page=True)
                width = page.evaluate("document.documentElement.scrollWidth")
                if width > w:
                    logs.append(f"[layout] page is {width}px wide in a {w}px viewport")
            except Exception as exc:  # noqa: BLE001
                logs.append(f"[nav] {exc}")
            if logs:
                problems[f"{route}@{vp}"] = list(logs)
        ctx.close()
    browser.close()

print(json.dumps(problems, indent=1) if problems else "no console errors or overflow")
print("saved to", OUT)
