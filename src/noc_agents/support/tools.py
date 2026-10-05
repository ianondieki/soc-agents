"""The action agent's tools, exactly the contract's table (docs/SUPPORT_DESK.md "Tools").

| tool                     | does                                                        | auto limit |
|--------------------------|-------------------------------------------------------------|------------|
| ``lookup_account``       | reads plan, balances, recent transactions and bundles       | always |
| ``issue_refund``         | refunds airtime or a charge                                 | up to KES 500, else approval |
| ``reverse_mpesa``        | reverses a wrong-number transfer by its 10-character code   | within 24h, up to KES 5,000, recipient has not withdrawn; else approval |
| ``recredit_bundle``      | re-credits a bundle that expired early or failed to apply   | one per 30 days |
| ``link_incident``        | finds an open NOC incident for the town or region named     | always |
| ``update_ticket``        | sets status and appends a note                              | always |
| ``reset_network_settings`` | sends the device configuration SMS (APN, MMS)             | always |

Every tool has the same shape, ``run(env, args, approved) -> ToolOutcome``, and does three
things in order: **validate** its arguments (a bad argument is ``refused``, naming it),
apply **policy** (the limits come from ``config/support/policy.yaml``; over a limit is
``needs_approval`` unless a person has approved, a rule that cannot be approved past --
"one re-credit per 30 days" -- is ``refused``), then **act**. ``policy`` on the outcome says
in words which rule allowed, limited or refused the call; it is what the UI shows beside it.

**Nothing real happens.** The tools read demo fixtures (:mod:`accounts`) and the live
``incidents`` table; a refund's or a reversal's *effect* is the result recorded on the call.
Idempotency reads those records back: a code that was already reversed, or a charge already
refunded, is refused rather than paid twice -- the dangerous double-action a support bot must
never take -- and so is a second request while the first still waits for a person's approval.
``subject_ref`` is the key for that lookup. The checks are only as good as the isolation they
run under, so every caller runs a tool inside SQLite's write lock (``desk._write_lock``): two
complaints, or two approvals, about one transfer are serialised and the second sees the first.

**``fallback``** marks a refusal the desk can recover from safely, by letting the resolver
answer from the knowledge base: nothing on the account is eligible for a re-credit (so the
bundle was simply used), no refundable charge matches (so the charge was valid), no open
incident matches the place (so the outage article answers). Every other refusal and every
failure escalates with ``tool_failed``, because a person has to finish the job.

``link_incident`` is the desk's tie-in to the live NOC: it reads OPEN incidents for the
operator only (the WHERE clause, as every operator-owned read in this codebase), top-level
tickets only (a cascade child is represented by its parent), and grades each against the
place the customer named (:func:`link_strength`, docs/CLOSE_THE_LOOP.md decisions):

* ``site`` -- the place is in the incident's SITE NAME ("no network in Nakuru" -> Nakuru Rift HUB);
* ``county`` -- the place is the incident's county;
* ``wide_area`` -- the place is in the incident's region AND the incident is wide: a HUB or CORE
  site, or one with child sites down ("hakuna network Kayole" -> the Embakasi East Aggregation HUB);
* ``region`` -- in the region of a single site's outage only. WEAK: the customer may be nowhere
  near that site, so the complaint is NOT linked (it would be told "service is back" about an
  outage that was never theirs); the incident is returned as ``nearby_incident`` instead, and the
  reply says honestly that there is a known outage nearby.

Only the strong three link; the best grade wins, then the incident affecting the most users.
A title no longer counts as a site match: titles carry the region's label, so "Nairobi East"
named every ticket in the region.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.db.models import IncidentRow
from noc_agents.db.models_support import SupportComplaintRow, SupportToolCallRow
from noc_agents.support.accounts import MPESA_CODE_PATTERN, Account
from noc_agents.support.policy import SupportPolicy
from noc_agents.support.text import mask_msisdn, mask_msisdn_staff, normalise
from noc_agents.support.vocab import STATUSES

#: Incident statuses that mean "engineers are no longer working on it".
CLOSED_INCIDENT_STATUSES: frozenset[str] = frozenset({"RESTORED", "CLOSED", "CANCELLED"})
MAX_NOTE_CHARS = 1000
_CODE = re.compile(MPESA_CODE_PATTERN)


@dataclass(frozen=True)
class ToolEnv:
    """Everything a tool may read. Built once per complaint by the action agent."""

    session: Session
    operator_id: str
    msisdn: str
    account: Account | None
    policy: SupportPolicy
    now: datetime
    #: Set when a person approves a parked call: that call is the one running, so it does not
    #: count as "another call already waiting for approval".
    approving_call_id: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    status: str  # ok | refused | needs_approval | failed
    result: dict[str, Any] | None
    policy: str
    subject_ref: str | None = None
    fallback: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    does: str
    auto_limit: str
    run: Callable[[ToolEnv, dict[str, Any], bool], ToolOutcome] = field(repr=False)


class ToolArgError(ValueError):
    """An argument failed validation; the message names it."""


# ------------------------------------------------------------------------------ helpers


def _refused(policy: str, *, subject_ref: str | None = None, fallback: bool = False) -> ToolOutcome:
    return ToolOutcome("refused", None, policy, subject_ref=subject_ref, fallback=fallback)


def _require(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value in (None, ""):
        raise ToolArgError(f"argument '{name}' is required")
    return value


def _account(env: ToolEnv) -> Account:
    if env.account is None:
        raise ToolArgError("there is no account on record for this number")
    return env.account


def _done_before(env: ToolEnv, tool: str, *, subject_ref: str | None = None, since: datetime | None = None) -> SupportToolCallRow | None:
    """The latest successful call of ``tool`` for this customer (optionally on ``subject_ref``, after ``since``)."""
    stmt = (
        select(SupportToolCallRow)
        .join(SupportComplaintRow, SupportComplaintRow.id == SupportToolCallRow.complaint_id)
        .where(
            SupportComplaintRow.operator_id == env.operator_id,
            SupportComplaintRow.msisdn == env.msisdn,
            SupportToolCallRow.tool == tool,
            SupportToolCallRow.status.in_(("ok", "approved")),
        )
        .order_by(SupportToolCallRow.at.desc())
        .limit(1)
    )
    if subject_ref is not None:
        stmt = stmt.where(SupportToolCallRow.subject_ref == subject_ref)
    if since is not None:
        stmt = stmt.where(SupportToolCallRow.at >= since)
    return env.session.scalar(stmt)


def _waiting_before(env: ToolEnv, tool: str, subject_ref: str) -> SupportComplaintRow | None:
    """The complaint on which ``tool`` on ``subject_ref`` already waits for a person's approval, if any.

    Two complaints about the same transfer must not park two reversals of it: a person approving
    both would be paying twice but for the second call's refusal. The second is refused at once,
    pointing at the first.
    """
    stmt = (
        select(SupportComplaintRow)
        .join(SupportToolCallRow, SupportComplaintRow.id == SupportToolCallRow.complaint_id)
        .where(
            SupportComplaintRow.operator_id == env.operator_id,
            SupportComplaintRow.msisdn == env.msisdn,
            SupportToolCallRow.tool == tool,
            SupportToolCallRow.subject_ref == subject_ref,
            SupportToolCallRow.status == "needs_approval",
        )
        .limit(1)
    )
    if env.approving_call_id is not None:
        stmt = stmt.where(SupportToolCallRow.id != env.approving_call_id)
    return env.session.scalar(stmt)


def _already(env: ToolEnv, tool: str, subject_ref: str, done: str) -> ToolOutcome | None:
    """Refused when ``tool`` already ran on ``subject_ref`` for this customer, or waits for approval."""
    if _done_before(env, tool, subject_ref=subject_ref):
        return _refused(f"{subject_ref} has already been {done}", subject_ref=subject_ref)
    waiting = _waiting_before(env, tool, subject_ref)
    if waiting is not None:
        return _refused(f"{subject_ref} is already waiting for a person's approval on {waiting.ref}", subject_ref=subject_ref)
    return None


def _kes(amount: int) -> str:
    return f"KES {amount:,}"


# -------------------------------------------------------------------------------- tools


def lookup_account(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    """Read-only: the customer's plan, balances, transactions, bundles and charges."""
    if env.account is None:
        return ToolOutcome("ok", {"found": False, "msisdn": mask_msisdn_staff(env.msisdn)}, "always allowed: read-only")
    return ToolOutcome("ok", env.account.summary(), "always allowed: read-only", subject_ref=env.account.account_ref)


def issue_refund(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    account = _account(env)
    charge_id = str(_require(args, "charge_id"))
    charge = account.charge(charge_id)
    if charge is None:
        raise ToolArgError(f"charge {charge_id} is not on this account")
    amount = int(args.get("amount_kes") or charge.amount_kes)
    if amount <= 0 or amount > charge.amount_kes:
        raise ToolArgError(f"amount_kes must be between 1 and the charge's {_kes(charge.amount_kes)}")
    if not charge.refundable:
        return _refused(f"'{charge.description}' was a delivered service, so it is not refundable", subject_ref=charge_id, fallback=True)
    if (refusal := _already(env, "issue_refund", charge_id, "refunded")) is not None:
        return refusal
    limit = env.policy.refund_auto_limit_kes
    if amount > limit and not approved:
        return ToolOutcome("needs_approval", None, f"refunds above {_kes(limit)} need a person's approval ({_kes(amount)} requested)", subject_ref=charge_id)
    result = {
        "refund_id": f"RF-{charge_id}",
        "charge_id": charge_id,
        "description": charge.description,
        "amount_kes": amount,
        "credited_to": "airtime",
        "new_airtime_balance_kes": account.airtime_balance_kes + amount,
    }
    rule = "approved by a person above the auto limit" if amount > limit else f"within the {_kes(limit)} auto limit"
    return ToolOutcome("ok", result, f"refund of {_kes(amount)} {rule}", subject_ref=charge_id)


def reverse_mpesa(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    account = _account(env)
    code = str(_require(args, "transaction_code")).strip().upper()
    if not _CODE.match(code):
        raise ToolArgError("transaction_code must be the 10-character M-PESA code")
    txn = account.transaction(code)
    if txn is None or txn.type != "sent":
        return _refused(f"no transfer with code {code} was sent from this number", subject_ref=code)
    if (refusal := _already(env, "reverse_mpesa", code, "reversed")) is not None:
        return refusal
    policy = env.policy
    over = []
    if txn.hours_ago > policy.reversal_window_hours:
        over.append(f"it is {txn.hours_ago:g}h old (auto limit {policy.reversal_window_hours}h)")
    if txn.amount_kes > policy.reversal_auto_limit_kes:
        over.append(f"{_kes(txn.amount_kes)} is above the {_kes(policy.reversal_auto_limit_kes)} auto limit")
    if txn.recipient_withdrawn:
        over.append("the recipient has already withdrawn the money")
    if over and not approved:
        return ToolOutcome("needs_approval", None, "reversal needs a person's approval: " + "; ".join(over), subject_ref=code)
    result = {
        "reversal_id": f"RV-{code}",
        "transaction_code": code,
        "amount_kes": txn.amount_kes,
        "counterparty": txn.counterparty,
        "credited_to": "M-PESA",
        "new_mpesa_balance_kes": account.mpesa_balance_kes + txn.amount_kes,
    }
    rule = "approved by a person: " + "; ".join(over) if over else (
        f"within {policy.reversal_window_hours}h and {_kes(policy.reversal_auto_limit_kes)}, recipient has not withdrawn"
    )
    return ToolOutcome("ok", result, rule, subject_ref=code)


def recredit_bundle(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    account = _account(env)
    bundle_id = str(_require(args, "bundle_id"))
    bundle = account.bundle(bundle_id)
    if bundle is None:
        raise ToolArgError(f"bundle {bundle_id} is not on this account")
    if bundle.status not in ("expired_early", "not_applied"):
        return _refused(f"bundle {bundle.name} ran its full course, so it is not eligible", subject_ref=bundle_id, fallback=True)
    cooldown = env.policy.recredit_cooldown_days
    recent = _done_before(env, "recredit_bundle", since=env.now - timedelta(days=cooldown))
    last_days = account.last_recredit_days_ago
    if recent is not None or (last_days is not None and last_days < cooldown):
        when = f"{last_days:g} days ago" if recent is None else f"within the last {cooldown} days"
        return _refused(f"one re-credit per {cooldown} days; the last one was {when}", subject_ref=bundle_id)
    waiting = _waiting_before(env, "recredit_bundle", bundle_id)
    if waiting is not None:
        return _refused(f"{bundle_id} is already waiting for a person's approval on {waiting.ref}", subject_ref=bundle_id)
    result = {
        "bundle_id": bundle_id,
        "name": bundle.name,
        "size_mb": bundle.size_mb,
        "valid_for_hours": bundle.validity_hours,
        "was": bundle.status,
    }
    return ToolOutcome("ok", result, f"first re-credit in {cooldown} days", subject_ref=bundle_id)


#: How a complaint was linked to its incident (``support_complaints.link_strength``). The first three
#: are what ``link_incident`` may link on its own; ``person`` is a person's link (a staff member, or
#: the confirmer of a surge); ``region`` is never linked (the module docstring).
LINK_STRENGTHS: tuple[str, ...] = ("site", "county", "wide_area", "person", "region")
STRONG_LINKS: frozenset[str] = frozenset({"site", "county", "wide_area"})
_RANK = {"site": 3, "county": 2, "wide_area": 1, "region": 0}
WIDE_SITE_TYPES: frozenset[str] = frozenset({"HUB", "CORE"})


def link_strength(incident: IncidentRow, place: str, regions: tuple[str, ...]) -> str | None:
    """How well ``place`` (normalised) points at ``incident``: site, county, wide_area, region or None."""
    if place and re.search(r"(?<!\w)" + re.escape(place) + r"(?!\w)", normalise(incident.site_name or "")):
        return "site"
    if place and place == normalise(incident.county or ""):
        return "county"
    if incident.region_code in regions:
        wide = (incident.site_type or "").upper() in WIDE_SITE_TYPES or (incident.child_sites_down or 0) >= 1
        return "wide_area" if wide else "region"
    return None


def best_link(session: Session, operator_id: str, place: str, regions: tuple[str, ...]
              ) -> tuple[str | None, IncidentRow | None]:
    """The best open, top-level incident for ``place``: ``(strength, incident)``, or ``(None, None)``.
    The strength may be ``region`` (weak): the caller decides what a weak match may do."""
    open_incidents = session.scalars(
        select(IncidentRow).where(
            IncidentRow.operator_id == operator_id,
            IncidentRow.status.not_in(CLOSED_INCIDENT_STATUSES),
            IncidentRow.parent_incident_id.is_(None),
        )
    ).all()
    graded = [(strength, inc) for inc in open_incidents
              for strength in [link_strength(inc, place, regions)] if strength is not None]
    if not graded:
        return None, None
    return max(graded, key=lambda g: (_RANK[g[0]], g[1].users_affected or 0, g[1].created_at))


def link_incident(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    place = normalise(str(_require(args, "place")))
    regions = tuple(args.get("regions") or ())
    strength, inc = best_link(env.session, env.operator_id, place, regions)
    if inc is None:
        return ToolOutcome(
            "ok", {"found": False, "place": place, "regions": list(regions)},
            "always allowed: no open incident matches the place named", fallback=True,
        )
    if strength not in STRONG_LINKS:
        nearby = {"incident_id": inc.id, "incident_number": inc.incident_number, "site_name": inc.site_name,
                  "status": inc.status, "region_code": inc.region_code}
        return ToolOutcome(
            "ok", {"found": False, "place": place, "regions": list(regions), "nearby_incident": nearby},
            f"always allowed: only a single-site outage in the region ({inc.incident_number}), too weak to link",
            fallback=True,
        )
    result = {
        "found": True,
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "title": inc.title,
        "status": inc.status,
        "site_name": inc.site_name,
        "region_code": inc.region_code,
        "place": place,
        "match": strength,
        "link_strength": strength,
    }
    return ToolOutcome("ok", result, f"always allowed: matched on {strength.replace('_', ' ')}", subject_ref=inc.id)


def update_ticket(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    status = str(_require(args, "status"))
    if status not in STATUSES:
        raise ToolArgError(f"status must be one of {list(STATUSES)}")
    note = str(_require(args, "note")).strip()
    if len(note) > MAX_NOTE_CHARS:
        raise ToolArgError(f"note must be at most {MAX_NOTE_CHARS} characters")
    return ToolOutcome("ok", {"status": status, "note": note}, "always allowed")


def reset_network_settings(env: ToolEnv, args: dict[str, Any], approved: bool = False) -> ToolOutcome:
    device = args.get("device") or (env.account.device if env.account else None)
    result = {
        "to": mask_msisdn(env.msisdn),
        "device": device,
        "settings": ["internet (APN)", "MMS"],
        "delivery": "recorded only: the demo never sends an SMS",
    }
    return ToolOutcome("ok", result, "always allowed")


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("lookup_account", "reads the customer's plan, balance, recent transactions and bundles", "always", lookup_account),
        ToolSpec("issue_refund", "refunds airtime or a charge", "up to KES 500, else approval", issue_refund),
        ToolSpec("reverse_mpesa", "reverses a wrong-number transfer by its 10-character code",
                 "within 24h and up to KES 5,000, recipient has not withdrawn; else approval", reverse_mpesa),
        ToolSpec("recredit_bundle", "re-credits a data bundle that expired early or failed to apply", "one per 30 days", recredit_bundle),
        ToolSpec("link_incident", "finds an open NOC incident for the town or region named and links it", "always", link_incident),
        ToolSpec("update_ticket", "sets status and appends a note", "always", update_ticket),
        ToolSpec("reset_network_settings", "sends the device configuration SMS (APN, MMS)", "always", reset_network_settings),
    )
}


def run_tool(name: str, env: ToolEnv, args: dict[str, Any], *, approved: bool = False) -> ToolOutcome:
    """Run one tool. Never raises: a bad argument is ``refused``, an error is ``failed``."""
    spec = TOOLS.get(name)
    if spec is None:
        return _refused(f"no such tool: {name}")
    try:
        return spec.run(env, args, approved)
    except ToolArgError as exc:
        return _refused(f"invalid arguments: {exc}")
    except Exception as exc:  # noqa: BLE001 -- a tool error escalates (tool_failed); it never crashes the desk
        return ToolOutcome("failed", None, f"the tool raised {type(exc).__name__}")
