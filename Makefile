.PHONY: install test demo run run-ui build-ui demo-up screenshots

install:
	python -m pip install -e ".[dev]"
	cd frontend && npm install

test:
	python -m pytest -q

demo:
	python -m noc_agents.scripts.demo_safaricom

run:
	python -m uvicorn noc_agents.main:app --app-dir src --host 0.0.0.0 --port 8000 --reload --no-proxy-headers

run-ui:
	cd frontend && npm run dev

build-ui:
	cd frontend && npm run build

# One command for a showcase: build the UI and serve API + UI on :8000 with the live agent
# delay on (Linux/macOS; on Windows use scripts/run_all.ps1).
demo-up:
	bash scripts/run_all.sh

# Every route at 375 and 1440 px against a running stack (default http://127.0.0.1:8000),
# with a storm seeded first. Needs the playwright extra: pip install playwright
screenshots:
	python scripts/screenshots.py docs/screenshots
