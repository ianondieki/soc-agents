"""Phase 0 acceptance for the generated agent/MCP/LLM document.

``docs/AGENTS_MCP_LLM.md`` is rendered from the registry by ``scripts/render_agent_docs.py``.
It is committed so it can be read on GitHub without running anything, which means it can also
go stale the moment somebody edits the registry. These tests are what makes that impossible:
the committed bytes must equal a fresh render, and every agent name must appear in the file.

The renderer is imported as a module (``scripts`` is not a package, so this goes through
``importlib``) rather than shelled out: the suite stays fast and a failure shows a real diff
instead of a captured stdout blob.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from noc_agents.orchestrator.registry import agent_catalog

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "render_agent_docs.py"
DOC = ROOT / "docs" / "AGENTS_MCP_LLM.md"
REGEN = "C:\\Python313\\python.exe scripts/render_agent_docs.py --write"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_agent_docs", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so dataclasses/typing in the module resolve normally
    spec.loader.exec_module(module)
    return module


renderer = _load_renderer()


def test_the_generator_and_the_document_both_exist():
    assert SCRIPT.is_file(), SCRIPT
    assert DOC.is_file(), f"{DOC} is missing — run: {REGEN}"


def test_committed_document_matches_a_fresh_render():
    """The one that matters: the committed file IS the renderer's output, byte for byte."""
    fresh = renderer.render()
    committed = renderer.read_committed(DOC)
    if committed != fresh:
        crlf = "\r\n" in committed and "\r\n" not in fresh
        pytest.fail(
            "docs/AGENTS_MCP_LLM.md is stale: it does not match the registry it is generated from.\n"
            f"Regenerate it with:\n    {REGEN}\n"
            + ("(The committed file has CRLF line endings; the document is written with LF.)\n" if crlf else "")
            + "\n"
            + renderer.diff(committed, fresh, DOC)
        )


def test_document_mentions_every_agent_in_the_catalog():
    """An agent added to the registry without regenerating the doc fails here, by name."""
    text = renderer.read_committed(DOC)
    missing = [entry["name"] for entry in agent_catalog() if entry["name"] not in text]
    assert not missing, f"agents absent from docs/AGENTS_MCP_LLM.md: {missing} — run: {REGEN}"


def test_check_mode_passes_against_the_committed_document():
    """``--check`` is what CI runs; it must agree with the test above."""
    assert renderer.main(["--check"]) == 0, f"--check rejected the committed document — run: {REGEN}"


def test_rendering_is_deterministic():
    """Same registry in, identical bytes out — no clock, no set iteration, no locale."""
    assert renderer.render().encode("utf-8") == renderer.render().encode("utf-8")


def test_write_round_trip_is_byte_identical_and_stays_lf(tmp_path):
    """The Windows trap: a CRLF round-trip would make --check fail on an untouched file."""
    target = tmp_path / "AGENTS_MCP_LLM.md"
    written = renderer.write(target)
    raw = target.read_bytes()
    assert b"\r\n" not in raw, "the renderer wrote CRLF line endings"
    assert raw == written.encode("utf-8")
    assert renderer.read_committed(target) == renderer.render()
    assert renderer.check(target) == 0
