#!/usr/bin/env bash
# The public demo build (docs/DEMO_BUILD.md): record the real backend, build the console in demo
# mode, and leave a static site in frontend/dist-demo that runs with no server, from any path.
#
#   bash scripts/demo/build.sh              # then serve frontend/dist-demo with any static server
#   PYTHON=/path/to/python bash scripts/demo/build.sh
#
# Writes frontend/dist-demo/page.html too: the same entry page as a body fragment, for a host that
# wraps pages in its own document.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
fi

(cd frontend && npm run build:demo)
"$PY" scripts/demo/record.py frontend/dist-demo/demo-data/recording.json
"$PY" scripts/demo/fragment.py frontend/dist-demo/index.html frontend/dist-demo/page.html
echo "Demo built in frontend/dist-demo. Try it: (cd frontend/dist-demo && $PY -m http.server 8090), then open http://127.0.0.1:8090/"
