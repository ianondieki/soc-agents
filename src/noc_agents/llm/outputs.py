"""Pydantic shapes the model must fill (structured outputs). No extras, no validators:
the deterministic checks live in ``assist.py`` so a template fallback is one place."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Hypothesis(BaseModel):
    cause: str
    likelihood: Literal["high", "medium", "low"]
    evidence: list[str]


class RootCauseAnalysis(BaseModel):
    summary: str
    hypotheses: list[Hypothesis]
    recommended_checks: list[str]


class ExecBriefDraft(BaseModel):
    body: str
