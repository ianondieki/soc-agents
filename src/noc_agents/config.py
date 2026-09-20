from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
OPERATORS_DIR = CONFIG_DIR / "operators"


def _load_dotenv() -> None:
    """Load .env from project root if python-dotenv is installed.

    NOC_SKIP_DOTENV=1 short-circuits the whole thing (tests set it) so a stray
    .env on a developer machine can never leak into a run.
    """
    if (os.getenv("NOC_SKIP_DOTENV") or "").strip().lower() in ("1", "true", "yes", "on"):
        return
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        # Minimal parser so demos work without extra dependency
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()


class PriorityThresholds(BaseModel):
    P4_max_users: int = 49999
    P3_max_users: int = 249999
    P2_max_users: int = 999999


class SlaBand(BaseModel):
    ack: int
    restore: int
    note_interval: int


class RegionConfig(BaseModel):
    label: str
    rnio: str
    fe_oncall: str
    description: str = ""
    coverage_areas: list[str] = Field(default_factory=list)
    counties: list[str] = Field(default_factory=list)
    hub_sites_hint: list[str] = Field(default_factory=list)
    radio_oem: str = "MIXED"
    power_msp_primary: str = ""
    tx_msp_primary: str = ""


class MpesaRiskConfig(BaseModel):
    enable: bool = False
    site_types: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)


class ShiftConfig(BaseModel):
    start: str
    end: str
    handover_to: str
    distribution_list: list[str] = Field(default_factory=list)


class OperatorConfig(BaseModel):
    operator_id: str
    display_name: str
    incident_prefix: str
    problem_prefix: str
    numbering_style: str = "inc9"  # inc9 => INC + 6 digits (9 chars total)
    timezone: str = "Africa/Nairobi"
    autonomy_level: str = "L2_GUARDED"
    locale_notes: str = ""
    network_stats: dict[str, Any] = Field(default_factory=dict)
    priority_thresholds: PriorityThresholds
    site_type_priority_floor: dict[str, str]
    p1_force_rules: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)
    sla_minutes: dict[str, SlaBand]
    region_sla_note_multiplier: dict[str, float] = Field(default_factory=dict)
    assignment_matrix: dict[str, list[str]] = Field(default_factory=dict)
    assignment_by_region: dict[str, dict[str, Any]] = Field(default_factory=dict)
    msp_region_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    msp_contacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    field_engineers_demo: dict[str, list[str]] = Field(default_factory=dict)
    regions: dict[str, RegionConfig]
    shifts: dict[str, ShiftConfig]
    management_distribution_list: list[str] = Field(default_factory=list)
    # Outbound recipient register: ``{recipients_ref: [address, ...]}`` (§7.0.2). Outbox
    # payloads carry a REF, never an address; ``services/notify.resolve_recipients`` looks the
    # ref up here at dispatch time. Additive with a ``{}`` default on purpose: a profile that
    # names no recipients still loads, it simply cannot send to a named ref — and a ref that
    # is missing or empty REFUSES the dispatch instead of falling back to DEMO_EMAIL_TO.
    notification_recipients: dict[str, list[str]] = Field(default_factory=dict)
    recurrence: dict[str, Any] = Field(default_factory=dict)
    broadcast: dict[str, Any] = Field(default_factory=dict)
    mpesa_risk: MpesaRiskConfig = Field(default_factory=MpesaRiskConfig)
    user_estimate_defaults: dict[str, int] = Field(default_factory=dict)
    tt_categories: dict[str, str] = Field(default_factory=dict)
    site_class_by_type: dict[str, str] = Field(default_factory=dict)


class AppSettings(BaseModel):
    operator_profile: str = "safaricom"
    timezone: str = "Africa/Nairobi"
    database_url: str = "sqlite:///./data/noc_agents.db"
    autonomy_level: str | None = None
    operator: OperatorConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings(profile: str | None = None) -> AppSettings:
    default = _load_yaml(CONFIG_DIR / "default.yaml")
    op_id = (profile or os.getenv("OPERATOR_PROFILE") or default.get("operator_profile") or "safaricom").lower()
    op_path = OPERATORS_DIR / f"{op_id}.yaml"
    if not op_path.exists():
        raise FileNotFoundError(f"Operator profile not found: {op_path}")
    op_raw = _load_yaml(op_path)
    operator = OperatorConfig.model_validate(op_raw)
    autonomy = os.getenv("AUTONOMY_LEVEL") or default.get("autonomy_level") or operator.autonomy_level
    operator.autonomy_level = autonomy
    db = os.getenv("DATABASE_URL") or default.get("database_url") or "sqlite:///./data/noc_agents.db"
    # Ensure relative sqlite path resolves under project root
    if db.startswith("sqlite:///./"):
        rel = db.replace("sqlite:///./", "")
        abs_path = (ROOT / rel).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        db = f"sqlite:///{abs_path.as_posix()}"
    return AppSettings(
        operator_profile=op_id,
        timezone=default.get("timezone", "Africa/Nairobi"),
        database_url=db,
        autonomy_level=autonomy,
        operator=operator,
    )


def clear_settings_cache() -> None:
    get_settings.cache_clear()
