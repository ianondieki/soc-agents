"""Placeholder for the outbox dead-letter view and retry routes (spec 10.4). Filled in by the lane that owns this file."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["outbox_admin"])
