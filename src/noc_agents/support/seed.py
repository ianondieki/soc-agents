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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from noc_agents.db.models import utcnow
from noc_agents.support.context import SupportContext
from noc_agents.support.desk import claim, process_complaint, resolve_case


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

RESOLVE_REPLY = (
    "Hi Chebet, I am sorry for the trouble with your Business 50GB bundle. I have asked our business team to review the "
    "bundle and your usage today, and I will call you tomorrow morning with the outcome."
)


def seed_demo(session: Session, *, operator_id: str, actor: str, ctx: SupportContext,
              now: datetime | None = None) -> int:
    """Run :data:`SEED` through the desk; returns how many complaints were created."""
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
    return created
