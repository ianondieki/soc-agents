.PHONY: install test demo run run-ui build-ui

install:
	python -m pip install -e ".[dev]"
	cd frontend && npm install

test:
	python -m pytest -q

demo:
	python -m noc_agents.scripts.demo_safaricom

run:
	python -m uvicorn noc_agents.main:app --app-dir src --host 0.0.0.0 --port 8000 --reload

run-ui:
	cd frontend && npm run dev

build-ui:
	cd frontend && npm run build
