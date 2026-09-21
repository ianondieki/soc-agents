"""Guardrail G4, the grep layer: nothing on the incident hot path may import an I/O module.

Spec §2, G4: *"Nothing new on the ``POST /api/v1/events`` hot path does I/O. No network, no
LLM, no MCP, no PDF parsing, no SMTP inside ``run_incident_lifecycle``."* It is enforced in two
layers, and the spec is explicit about why there are two:

* a **runtime** test that blocks every socket during ``process_event`` and asserts the run still
  SUCCEEDS -- ``tests/system/test_degraded_mode.py``, which has existed since Phase 1;
* a **grep** test that ``src/noc_agents/agents/*.py`` and ``orchestrator/runner.py`` "import
  neither ``httpx`` nor ``anthropic`` nor ``mcp`` nor any poller/adapter module".

Until this file, the second layer did not exist -- the Phase 4 conformance audit found the gap.
The spec's own caveat, *"a function-local import defeats a grep; a blocked socket does not"*,
turned out to cut the other way as well: the runtime layer was green while a function-local
``from noc_agents.pollers.weather import ...`` sat inside ``agents/enrich.py``. No socket was
ever opened (the call is one indexed SELECT), so the socket test had nothing to catch; but the
poller module imports ``adapters/weather.py``, which imports ``httpx`` at module level, so the
whole network stack was being loaded from the middle of ``run_incident_lifecycle``. The first
run of this test is what found it. The fix was ``services/signals.py``.

So this is an **AST walk, not a grep**, and it deliberately descends into function bodies: the
lazy import is the case most likely to be written by accident, and the one a line-anchored grep
for ``^import`` would miss.

If a test here fails, do not add the import and do not add an exemption. A hot-path agent may
*read rows that an out-of-band job wrote*; put the read in a module with no network imports
(``services/signals.py`` is the model) and have the poller write the rows.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "noc_agents"

# Every module that executes inside run_incident_lifecycle: the twelve agents, plus the runner
# that drives them. The spec names exactly this set.
HOT_PATH = sorted((SRC / "agents").glob("*.py")) + [SRC / "orchestrator" / "runner.py"]

# Third-party modules that exist to talk to something outside the process.
FORBIDDEN_TOP_LEVEL = {
    "httpx", "requests", "urllib3", "aiohttp", "websockets",  # HTTP / sockets
    "anthropic", "openai", "mcp",  # LLM and MCP clients
    "smtplib", "imaplib", "poplib", "ftplib", "telnetlib", "socket", "ssl",  # stdlib network
    "pdfplumber", "feedparser", "africastalking",  # the spec's named offenders
}

# Our own packages that are out-of-band by definition. `adapters` wraps external services,
# `pollers` fetches on a schedule, `llm` builds model clients, `tools` is the MCP surface.
FORBIDDEN_INTERNAL_PREFIXES = (
    "noc_agents.adapters",
    "noc_agents.pollers",
    "noc_agents.llm",
    "noc_agents.tools",
)


def imports_anywhere(path: Path) -> list[tuple[int, str]]:
    """``(lineno, module)`` for every import in ``path`` -- module level AND inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def is_forbidden(module: str) -> bool:
    if module.split(".")[0] in FORBIDDEN_TOP_LEVEL:
        return True
    return any(module == p or module.startswith(p + ".") for p in FORBIDDEN_INTERNAL_PREFIXES)


def test_the_hot_path_set_is_the_one_the_spec_names() -> None:
    """Guard the guard: if agents/ is ever emptied or moved, this file must not pass vacuously."""
    names = {p.name for p in HOT_PATH}
    assert "runner.py" in names
    for agent in ("enrich.py", "correlate.py", "severity.py", "assign.py", "ticket.py",
                  "broadcast.py", "hitl.py", "ledger.py", "recurrence.py"):
        assert agent in names, f"{agent} is missing from the hot-path set"
    assert len(HOT_PATH) >= 13


@pytest.mark.parametrize("path", HOT_PATH, ids=lambda p: p.name)
def test_no_hot_path_module_imports_an_io_module_even_inside_a_function(path: Path) -> None:
    offenders = [f"{path.name}:{lineno}  import {mod}" for lineno, mod in imports_anywhere(path) if is_forbidden(mod)]
    assert not offenders, (
        "guardrail G4: a hot-path module imports something that can do I/O --\n  "
        + "\n  ".join(offenders)
        + "\nA hot-path agent may READ rows an out-of-band job wrote. Put the read in a module"
        " with no network imports (services/signals.py is the model); do not import the poller."
    )


def lazy_internal_imports(path: Path) -> set[str]:
    """``noc_agents.*`` modules imported from INSIDE a function or method body in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(scope):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.startswith("noc_agents."):
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("noc_agents."))
    return found


def test_every_module_the_hot_path_imports_lazily_is_free_of_network_clients() -> None:
    """The transitive half, for the imports the spec is actually worried about.

    The AST rule above is about what a hot-path file *names*. This one is about what ends up
    LOADED, and it is scoped to **function-local** imports on purpose. Those are how optional,
    advisory reads enter the hot path (the weather cache today; agent memory next), they are the
    case the spec singles out -- "a function-local import defeats a grep" -- and they are the
    ones written by someone who was thinking about the feature, not about the import graph.
    Each is imported in a fresh interpreter, which must then have no network client loaded. It
    is what would have caught the ``pollers.weather`` bug even one module removed.

    What this deliberately does NOT claim: that nothing reachable from the hot path loads a
    network module. That is false, and by design. ``orchestrator/outbox.py`` is imported at
    module level by the agents that enqueue messages, and it is BOTH the transactional queue and
    the dispatcher that owns the transmitters (spec §4.2.3, "the outbox is the only path out"),
    so it imports ``smtplib`` and the SMTP/LLM adapters itself. Loading a module performs no
    I/O; the hot path calls ``enqueue`` -- one INSERT -- and the transmitters run after commit,
    out of band. The guarantee that nothing is *sent* from inside the lifecycle is the blocked
    socket in ``tests/system/test_degraded_mode.py``, not this file. Splitting the outbox in two
    to make a stricter version of this test pass would be a refactor of golden-path code in
    exchange for no change in behaviour, so the honest scope is stated here instead.
    """
    import os

    lazy = sorted(set().union(*(lazy_internal_imports(p) for p in HOT_PATH)))
    assert "noc_agents.services.signals" in lazy, (
        "the scan no longer sees ENRICH's lazy import of services.signals -- either it moved, or"
        " lazy_internal_imports() is broken and this test is passing vacuously"
    )
    leaks: dict[str, str] = {}
    for module in lazy:
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys, importlib; importlib.import_module({module!r}); "
             "print('LEAKED=' + ','.join(m for m in ('httpx', 'smtplib', 'anthropic', 'mcp',"
             " 'pdfplumber', 'feedparser') if m in sys.modules))"],
            capture_output=True,
            text=True,
            env={**os.environ, "NOC_SKIP_DOTENV": "1"},
        )
        assert result.returncode == 0, f"importing {module} failed:\n{result.stderr[-1500:]}"
        leaked = result.stdout.strip().rsplit("LEAKED=", 1)[-1]
        if leaked:
            leaks[module] = leaked
    assert not leaks, (
        "guardrail G4: a module the hot path imports lazily loads a network client --\n  "
        + "\n  ".join(f"{m}  ->  {what}" for m, what in sorted(leaks.items()))
        + "\nSplit the read side into a network-free module, as services/signals.py was split"
        " from pollers/weather.py."
    )


def test_the_signal_reader_is_importable_without_the_network_stack() -> None:
    """``services/signals.py`` exists to be the network-free half. Hold it to that."""
    import os

    result = subprocess.run(
        [sys.executable, "-c", "import sys, noc_agents.services.signals; print('HTTPX=' + str('httpx' in sys.modules))"],
        capture_output=True,
        text=True,
        env={**os.environ, "NOC_SKIP_DOTENV": "1"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("HTTPX=False"), (
        "services/signals.py now loads httpx. It is the module ENRICH reads the weather cache"
        " through, from inside run_incident_lifecycle; it must import no adapter and no poller."
    )


def test_the_poller_still_re_exports_the_readers_for_out_of_band_callers() -> None:
    """The move must not have broken the callers that were never the problem."""
    from noc_agents.pollers import weather as poller
    from noc_agents.services import signals

    for name in ("is_stale", "staleness", "latest_signal", "latest_error", "weather_risk_for_region"):
        assert getattr(poller, name) is getattr(signals, name), name
