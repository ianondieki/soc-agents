"""``POST /api/v1/support/demo/seed``: a dozen realistic complaints, so the desk has a queue.

Every complaint goes through :func:`desk.process_complaint` -- the seed shows the real
pipeline's decisions, it does not write rows that pretend to be them -- backdated over the
last hour and a half across all five channels, in English, Kiswahili and Sheng. Between them
they reach every route and every status a complaint can be in without a person, and then two
human actions are applied (one fraud case claimed, one angry high-value case resolved) so the
queue also shows ``in_progress`` and ``resolved``.

The two outage complaints (Nakuru, Kayole) link to the live storm incidents when
``POST /api/v1/demo/rain-storm`` has been run first; on a quiet network they are answered from
the outage article instead. Nothing is invented to make the link happen.

Seeding twice inside two minutes creates nothing new (the desk's dedupe); after that it adds
a fresh set, and the repeat-complaint rule will start to notice the same numbers.

**Close the loop** (docs/CLOSE_THE_LOOP.md "Demo"). The seed also leaves:

* two complaints on EACH open storm incident (English and Kiswahili, two numbers), plus a second
  complaint from one of those numbers, for the repeat count. The place each names is one that
  ``link_incident`` -- the real tool, asked read-only -- maps to that incident, so the desk links
  them itself. An incident no customer-typed place can reach (the storm's P3 ring node shares its
  county and region with the far bigger Embakasi HUB, and the tool's tie-break picks the HUB) gets
  two call-centre complaints that name no place instead, linked by a recorded step in the seeding
  person's name ("the caller is served by that site") -- visible in the trace, never invented
  silently. Restoring that P3 then tells its customers at once, naming the incident's area;
* three complaints from three numbers about Rongai, where nothing is open: the third opens one
  "Possible outage" card (``surge.observe``, which the seeder runs after every complaint, as the
  API does; the eval runner never does).

On a quiet network (no storm) only the Rongai burst is added: nothing is linked to anything that
is not open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.db.models import IncidentRow, utcnow
from noc_agents.db.models_support import SupportComplaintRow
from noc_agents.services.scenarios import RAIN_STORM_EVENTS
from noc_agents.support import desk, surge
from noc_agents.support.context import SupportContext
from noc_agents.support.desk import claim, process_complaint, resolve_case
from noc_agents.support.loop import place_label
from noc_agents.support.tools import CLOSED_INCIDENT_STATUSES, ToolEnv, run_tool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedComplaint:
    text: str
    msisdn: str
    name: str | None
    channel: str
    minutes_ago: int
    then: str | None = None  # "claim" | "resolve": the person's action applied after the desk


SEED: tuple[SeedComplaint, ...] = (
    SeedComplaint("No network in Nakuru since morning, I cannot make any calls.", "0700001023", "Wafula Barasa", "sms", 95),
    SeedComplaint("Manze hakuna network huku Kayole tangu saa nne, kuna shida gani?", "0700001134", "Nyambura", "social", 82),
    SeedComplaint("I sent KES 1,500 to the wrong number this morning. The code is SJK4H7QW2L, please reverse it.",
                  "0700000412", "Wanjiku Kamau", "app", 71),
    SeedComplaint("Nilinunua Weekly 2GB jana lakini bundles zimeisha mapema, sijatumia hata nusu.", "0700000345",
                  "Kiprotich", "web", 64),
    SeedComplaint("Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Tafadhali rudisha haraka.", "0700000118",
                  "Otieno Odhiambo", "call_centre", 56),
    SeedComplaint("Someone did a SIM swap on my line last night and withdrew KES 20,000 from my M-PESA. I did not authorise this!",
                  "0700001245", "Ochieng Okoth", "call_centre", 49, then="claim"),
    SeedComplaint("Nimechoka na hizi sms za ajabu kila siku, how do I stop these premium messages?", "0711000202",
                  None, "web", 41),
    SeedComplaint("My postpaid bill this month is KES 4,800, much higher than usual. Please explain it.", "0711000214",
                  "Grace Wekesa", "app", 34),
    SeedComplaint("I have been charged KES 1,500 for a betting tips subscription I never subscribed to. I want a refund.",
                  "0700000789", "Omondi Ouma", "web", 28),
    SeedComplaint("You people are THIEVES!! My Business 50GB bundle expired early again, this is useless service.",
                  "0700000890", "Chebet Jepkosgei", "social", 21, then="resolve"),
    SeedComplaint("How do I become an M-PESA agent and how much float do I need to start?", "0711000237", "Peter Kiplagat", "web", 14),
    SeedComplaint("I bought a new phone and mobile data is not working. Please send me the internet settings.", "0110000112",
                  "Juma Bakari", "app", 8),
    SeedComplaint("If you do not refund the KES 3,000 you deducted from my airtime I will report you to the CA.", "0711000226",
                  None, "sms", 3),
)

#: Complaints on each open storm incident, naming a place the tool maps to it: (text, channel).
OUTAGE_TEXTS: tuple[tuple[str, str], ...] = (
    ("No network in {place} since this morning, calls keep failing.", "web"),
    ("Hakuna mtandao {place} tangu asubuhi, siwezi kupiga simu.", "sms"),
)
#: The first outage number complains again, later: the repeat contact the Loop numbers count.
REPEAT_TEXT = "Still no network in {place}, it has been hours now."
#: For an incident no typed place reaches: call-centre complaints naming no place.
NO_PLACE_TEXTS: tuple[tuple[str, str], ...] = (
    ("No network on my line since this morning, calls are not going through.", "call_centre"),
    ("Hakuna network kwa simu yangu tangu asubuhi.", "call_centre"),
)
#: Three numbers about Rongai inside the surge window, with no incident open there.
SURGE_SEED: tuple[SeedComplaint, ...] = (
    SeedComplaint("No network in Rongai since 2pm, calls are not going through.", "0722900001", "Mercy Wanjiru", "app", 26),
    SeedComplaint("Hakuna network Rongai tangu saa nane.", "0722900002", None, "sms", 17),
    SeedComplaint("Rongai network is down, I cannot call or browse.", "0722900003", "Brian Otieno", "web", 9),
)

RESOLVE_REPLY = (
    "Hi Chebet, I am sorry for the trouble with your Business 50GB bundle. I have asked our business team to review the "
    "bundle and your usage today, and I will call you tomorrow morning with the outcome."
)


def seed_demo(session: Session, *, operator_id: str, actor: str, ctx: SupportContext,
              now: datetime | None = None) -> int:
    """Run :data:`SEED`, the storm's outage complaints and the Rongai burst through the desk;
    returns how many complaints were created."""
    now = now or utcnow()
    created = 0
    for item in SEED:
        at = now - timedelta(minutes=item.minutes_ago)
        result = process_complaint(session, operator_id=operator_id, body=item.text, msisdn=item.msisdn,
                                   name=item.name, channel=item.channel, ctx=ctx, now=at)
        if not result.created:
            continue
        created += 1
        row = result.complaint
        if item.then == "claim" and row.status == "escalated":
            claim(session, row, actor=actor, now=at + timedelta(minutes=4))
        elif item.then == "resolve" and row.status == "escalated":
            claim(session, row, actor=actor, now=at + timedelta(minutes=3))
            resolve_case(session, row, actor=actor, reply=RESOLVE_REPLY, note="Passed to the business team for a usage review.",
                         now=at + timedelta(minutes=9))
        _observe(session, row, ctx)
    created += _seed_outages(session, operator_id=operator_id, actor=actor, ctx=ctx, now=now)
    for item in SURGE_SEED:
        result = process_complaint(session, operator_id=operator_id, body=item.text, msisdn=item.msisdn,
                                   name=item.name, channel=item.channel, ctx=ctx,
                                   now=now - timedelta(minutes=item.minutes_ago))
        if result.created:
            created += 1
            _observe(session, result.complaint, ctx)
    return created


def _observe(session: Session, row: SupportComplaintRow, ctx: SupportContext) -> None:
    try:
        surge.observe(session, row, ctx=ctx)
    except Exception:  # noqa: BLE001 -- the complaint is filed; a surge failure is logged, not the seed's
        log.exception("support seed: surge observation failed for %s", row.ref)


def _open_storm_incidents(session: Session, operator_id: str) -> list[IncidentRow]:
    sites = {e.site_id for e in RAIN_STORM_EVENTS if not e.parent_hub_id}
    return list(session.scalars(select(IncidentRow).where(
        IncidentRow.operator_id == operator_id, IncidentRow.site_id.in_(sites),
        IncidentRow.status.not_in(CLOSED_INCIDENT_STATUSES), IncidentRow.parent_incident_id.is_(None),
    ).order_by(IncidentRow.incident_number)).all())


def _place_for(session: Session, inc: IncidentRow, operator_id: str, ctx: SupportContext, now: datetime) -> str | None:
    """A place a customer could type that ``link_incident`` (asked read-only) maps to ``inc``."""
    env = ToolEnv(session, operator_id, "+254722000000", None, ctx.policy, now)
    for mention in [*ctx.gazetteer.find(inc.site_name or ""), *ctx.gazetteer.find(inc.county or "")]:
        outcome = run_tool("link_incident", env, {"place": mention.name, "regions": list(mention.regions)})
        if (outcome.result or {}).get("incident_id") == inc.id:
            return mention.name
    return None


def _link_by_call_centre(session: Session, row: SupportComplaintRow, inc: IncidentRow, *, actor: str, at: datetime) -> None:
    """The seeding person, as a call-centre agent, links a no-place complaint to the site serving
    the caller: a ``human`` step in the trace, like every other person's action."""
    desk._locked(session, row)
    row.linked_incident_id, row.link_strength, row.updated_at = inc.id, "person", at
    desk._human_step(session, row, "linked_incident",
                     f"Linked to {inc.incident_number} ({inc.site_name}) by {actor}: the caller is served by that site.",
                     {"incident_id": inc.id, "incident_number": inc.incident_number, "linked_by": actor,
                      "why": "demo seed: the call-centre agent matched the caller's area to the site"}, at)
    session.commit()


def _seed_outages(session: Session, *, operator_id: str, actor: str, ctx: SupportContext, now: datetime) -> int:
    created = 0
    repeat_done = False
    for i, inc in enumerate(_open_storm_incidents(session, operator_id)):
        place = _place_for(session, inc, operator_id, ctx, now)
        session.commit()  # end the read transaction before the desk takes its write lock
        texts = [(text.format(place=place_label(place)), channel) for text, channel in OUTAGE_TEXTS] if place else NO_PLACE_TEXTS
        serial = int("".join(ch for ch in inc.incident_number if ch.isdigit())[-4:] or 0)
        for j, (text, channel) in enumerate(texts):
            msisdn = f"0722{serial:04d}{j + 1:02d}"  # stable per incident, so a re-seed dedupes
            at = now - timedelta(minutes=66 - 7 * i - 2 * j)
            result = process_complaint(session, operator_id=operator_id, body=text, msisdn=msisdn, channel=channel,
                                       ctx=ctx, now=at)
            if not result.created:
                continue
            created += 1
            if place is None and result.complaint.linked_incident_id is None:
                _link_by_call_centre(session, result.complaint, inc, actor=actor, at=at + timedelta(minutes=1))
            _observe(session, result.complaint, ctx)
            if place and j == 0 and not repeat_done:
                again = process_complaint(session, operator_id=operator_id, body=REPEAT_TEXT.format(place=place_label(place)),
                                          msisdn=msisdn, channel="app", ctx=ctx, now=at + timedelta(minutes=20))
                if again.created:
                    created += 1
                    repeat_done = True
                    _observe(session, again.complaint, ctx)
    return created
