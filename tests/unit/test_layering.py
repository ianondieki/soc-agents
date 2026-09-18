"""The import direction between the API layer and everything below it.

Phase 4 put the operator-scoping helpers (``_owned``, ``_operator_scoped``, ``_get_owned``)
and the RBAC allow-lists in ``noc_agents.api.deps``, because that is the one place the
operator clause is built and a second hand-written copy would drift from the first. Five
service modules then imported them -- ``services.clock_events``, ``services.dashboards``,
``services.evidence``, ``services.memory``, ``services.regulatory`` -- so the tree now has
real ``services -> api`` edges.

That is safe only while the arrow points ONE way. ``api.deps`` and ``api.auth`` are leaves:
they import fastapi, sqlalchemy, ``config`` and ``db.models``, and nothing else of ours. The
day one of them imports a service to do something convenient, the cycle closes and the app
stops importing at startup -- a hard crash on boot, in the module that every route depends
on, with a traceback that points at whichever module happened to be imported first rather
than at the edge that caused it.

Reviewing for that by eye does not work: the edge that closes the loop is always a
reasonable-looking one-line import in a file nobody is reviewing for layering. So it is
pinned here instead. If one of these tests fails, do not add the import -- move the thing
you wanted into a module below both, or pass it in as an argument.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "noc_agents"

# Modules that must stay leaves: importable with nothing of ours above them.
LEAF_API_MODULES = ("api/deps.py", "api/auth.py")

# The layers a leaf API module may never reach into.
FORBIDDEN_FOR_LEAVES = ("noc_agents.services", "noc_agents.agents", "noc_agents.orchestrator",
                        "noc_agents.pollers", "noc_agents.scheduler", "noc_agents.graph",
                        "noc_agents.main")


def imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, at module level or inside a function.

    Deliberately includes function-level imports: a deferred import still closes a cycle
    the moment it runs, and the codebase already uses them on purpose (``scheduler.loop``
    imports the weather job lazily for exactly this reason). A guard that only looked at
    the top of the file would miss the case most likely to happen by accident.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("rel", LEAF_API_MODULES)
def test_the_leaf_api_modules_never_import_a_layer_above_them(rel: str) -> None:
    path = SRC / rel
    assert path.exists(), f"{rel} has moved; update this guard rather than deleting it"
    offenders = sorted(
        mod
        for mod in imported_modules(path)
        for prefix in FORBIDDEN_FOR_LEAVES
        if mod == prefix or mod.startswith(prefix + ".")
    )
    assert not offenders, (
        f"{rel} imports {offenders}, which closes an import cycle: five service modules "
        f"import {rel.replace('/', '.').removesuffix('.py')} for the operator-scoping "
        f"helpers. Move what you need into a module below both, or pass it in as an "
        f"argument -- do not add this import."
    )


def test_api_deps_is_importable_entirely_on_its_own() -> None:
    """The guard above is static; this one is the dynamic proof.

    A fresh interpreter importing only ``noc_agents.api.deps`` must succeed. If some
    transitive import ever reaches back into the service layer this raises ImportError
    here rather than at 03:00 when the app restarts.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import noc_agents.api.deps; print('ok')"],
        capture_output=True,
        text=True,
        env={"NOC_SKIP_DOTENV": "1", "PATH": ""} | {k: v for k, v in __import__("os").environ.items()},
    )
    assert result.returncode == 0, f"importing api.deps alone failed:\n{result.stderr}"


def test_the_http_surface_never_writes_its_own_operator_clause() -> None:
    """No route handler may build ``model.operator_id == ...`` by hand.

    Operator isolation is a property of every QUERY, not of the deployment: both operators'
    rows share one database file, so the WHERE clause is the entire mechanism. Phase 2's
    conformance audit found six places where a hand-written clause was missing or wrong,
    including a Safaricom supervisor who could approve an Airtel HITL task.

    The scope here is the HTTP surface only -- ``main.py`` and ``api/routers/`` -- because
    that is where a missing clause becomes another operator's data rendered in a browser.
    Service and agent modules legitimately filter by ``operator_id`` when they take it as a
    parameter (the poller, the monitor, the templates registry all predate Phase 4 and do
    exactly that); forbidding it there would be a rule the codebase has never followed and
    would only teach people to route around this test.
    """
    surface = [SRC / "main.py", *sorted((SRC / "api" / "routers").glob("*.py"))]
    offenders: list[str] = []
    for path in surface:
        rel = path.relative_to(SRC).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):  # a comment explaining the rule is not a breach of it
                continue
            if ".operator_id ==" in stripped:
                offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "a route handler built its own operator clause:\n  "
        + "\n  ".join(offenders)
        + "\nUse _owned() / _get_owned() / _operator_scoped() from api/deps.py instead -- "
        "one implementation, one place for a reviewer to check."
    )
