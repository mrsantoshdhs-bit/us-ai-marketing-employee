"""The golden corpus for the AI reasoning core: labelled prospect replies, single-turn and multi-turn. Synthetic data
only (no real person or company).

Sources: the Phase 6 reply tests (tests/uat/test_p6_replies.py: the opt-outs with legal threats, "stop emailing and call
me", the controls that must stay warm, the plain refusals with typos, the business words that only look like opt-outs)
are reused as they are; the multi-turn conversations with requirements, budgets, timelines, objections and meeting
intent are written for this corpus.

Labels per case:
- intent: the truth, in aime.ai.schemas.INTENTS;
- stop: the writer asks to stop or complains (the opt-out / complaint gate: recall must be 100% with the rule floor);
- meeting: they ask for or agree to a call, meeting or demo;
- requirements: words that must appear in the extracted requirements (all of them, case-insensitive);
- budget / timeline: a phrase the extracted value must contain, or None when nothing is stated;
- objection: in aime.ai.schemas.OBJECTIONS.

A stop case's intent is "unsubscribe" or "complaint"; for scoring the two count as one class where noted."""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.uat.test_p6_replies import LEGAL_NO, WARM_CONTROL, WARM_STOP


@dataclass(frozen=True)
class Case:
    id: str
    text: str
    intent: str
    stop: bool = False
    meeting: bool = False
    requirements: tuple[str, ...] = ()
    budget: str | None = None
    timeline: str | None = None
    objection: str = "none"
    history: tuple[str, ...] = field(default_factory=tuple)
    subject: str = "Re: A quick question"
    source: str = "synthetic"


OUR_FIRST = ("Hi Dana,\n\nI read on your website that your agents track leads in spreadsheets. We build custom CRMs for "
             "property teams. Would a short call next week be useful?\n\nBest,\nSam")
OUR_SECOND = "Thanks Dana, that helps. What does your team use today, and who else would be involved in choosing?"


def _p6() -> list[Case]:
    out = []
    for i, t in enumerate(LEGAL_NO):
        complaint = any(w in t.lower() for w in ("lawyer", "attorney", "counsel", "ftc", "police", "sue"))
        out.append(Case(f"p6-legal-no-{i}", t.split("\n\n")[0], "complaint" if complaint else "unsubscribe", stop=True, source="p6"))
    for i, t in enumerate(WARM_STOP):  # the opt-out stands until the owner rules otherwise (the D2 tests)
        out.append(Case(f"p6-warm-stop-{i}", t, "unsubscribe", stop=True, meeting=True, source="p6"))
    for i, t in enumerate(WARM_CONTROL):
        out.append(Case(f"p6-warm-control-{i}", t, "interested", meeting=i >= 2, source="p6"))
    return out


SINGLE: list[Case] = [
    # plain refusals, typos and rude ones (p6 D7), and real opt-outs (p6 D2 controls)
    Case("p6-d7-fo", "Fuck off", "complaint", stop=True, source="p6"),
    Case("p6-d7-back", "Get off my back", "unsubscribe", stop=True, source="p6"),
    Case("p6-d7-typo1", "unsubscirbe", "unsubscribe", stop=True, source="p6"),
    Case("p6-d7-typo2", "rmeove me", "unsubscribe", stop=True, source="p6"),
    Case("p6-d7-typo3", "do not emial me", "unsubscribe", stop=True, source="p6"),
    Case("p6-d7-dnc", "dnc", "unsubscribe", stop=True, source="p6"),
    Case("p6-d7-counsel", "Direct all further communication to our legal counsel.", "complaint", stop=True, source="p6"),
    Case("p6-d7-ag", "Next email and I file a complaint with the attorney general.", "complaint", stop=True, source="p6"),
    Case("p6-d7-times", "How many times do I have to tell you to stop?", "unsubscribe", stop=True, source="p6"),
    Case("p6-d2-plain", "Stop emailing me.", "unsubscribe", stop=True, source="p6"),
    Case("p6-d2-nocall", "Don't email me, don't call me.", "unsubscribe", stop=True, source="p6"),
    Case("p6-d2-manager", "Stop emailing and call your manager, I want to complain.", "complaint", stop=True, source="p6"),
    Case("spam-report", "This is spam. I will report you to the FTC.", "complaint", stop=True),
    Case("remove-list", "Please remove me from your mailing list.", "unsubscribe", stop=True),
    # business words that only look like opt-outs (p6 review)
    Case("p6-review-dnc", "We follow DNC lists for our outbound team. How does your tool handle that?", "question",
         requirements=("dnc",), source="p6"),
    Case("p6-review-truck", "How many times a week would the truck stop at our warehouse?", "question", source="p6"),
    Case("p6-review-leads", "How many times have you helped a firm like ours stop losing leads?", "question", source="p6"),
    Case("p6-review-newaddr", "Please don't lose this address, it is my new one. Happy to talk next week.", "interested",
         meeting=True, timeline=None, source="p6"),
    # asks to talk (p6 D8), plain no
    Case("p6-d8-tue", "Call me Tuesday.", "interested", meeting=True, source="p6"),
    Case("p6-d8-invite", "Send me a calendar invite.", "interested", meeting=True, source="p6"),
    Case("p6-d8-no", "No thank you", "not_interested", source="p6"),
    Case("p6-d6-q2", "Not now, try Q2.", "objection", objection="timing", timeline="Q2", source="p6"),
    Case("p6-probe-how", "How did you get my email? Anyway, yes, let's talk Tuesday.", "interested", meeting=True, source="p6"),
    # synthetic single replies with extraction labels
    Case("pricing-15", "Interesting. What would this cost for 15 users?", "question", requirements=("15 users",),
         objection="none"),
    Case("budget-timeline", "We need a CRM for our 20 agents. Our budget is around $25k and we want to go live before March.",
         "interested", requirements=("crm",), budget="25k", timeline="march"),
    Case("vendor", "We use Salesforce already and are happy with it.", "objection", objection="competitor"),
    Case("no-need", "Thanks, but we don't really need this right now, our process works fine.", "not_interested",
         objection="no_need"),
    Case("authority", "I'm not the one who decides on software. That would be our COO.", "referral", objection="authority"),
    Case("referral", "Please talk to Sara Lopez, she runs operations here.", "referral"),
    Case("ooo", "I am out of the office until Monday, November 9 with limited access to email.", "out_of_office"),
    Case("human", "Are you a bot? I want to speak to a real person.", "question"),
    Case("demo", "Sounds useful. Can you show us a demo next Thursday afternoon?", "interested", meeting=True,
         timeline=None),
    Case("info", "Can you send me more information about how the AI agents work with our ticketing system?", "question",
         requirements=("ticketing",)),
    Case("price-objection", "Honestly this sounds expensive for a team our size.", "objection", objection="price"),
    Case("trust", "How do I know you won't leak our customer data? We've been burned before.", "objection",
         objection="trust", requirements=("data",)),
    Case("timing-q3", "Interesting, but we're in the middle of a merger. Reach out again in Q3.", "objection",
         objection="timing", timeline="q3"),
    Case("upset", "This is the third email this week. I find this really annoying.", "complaint", stop=True),
]

CONVERSATIONS: list[Case] = [
    Case("conv-crm-2", "Yes, we have 12 agents on spreadsheets. We'd want lead routing and reminders. Budget is maybe $30,000, "
         "and ideally live by end of Q1.", "interested", requirements=("routing", "reminders"), budget="30,000",
         timeline="q1", history=(OUR_FIRST,)),
    Case("conv-crm-3", "Mostly me and our ops lead, Priya. Can we do a call Wednesday at 2pm Eastern?", "interested",
         meeting=True, history=(OUR_FIRST, "Yes, we have 12 agents on spreadsheets.", OUR_SECOND)),
    Case("conv-hubspot", "We tried HubSpot last year and it was too generic. What would be different with you?", "question",
         objection="competitor", history=(OUR_FIRST,)),
    Case("conv-budget-none", "We don't have a budget for this until next fiscal year, which starts in July.", "objection",
         objection="timing", timeline="july", history=(OUR_FIRST, "Sounds interesting.", OUR_SECOND)),
    Case("conv-agents", "We'd like an AI agent that answers tenant maintenance emails and files tickets in Yardi. We have "
         "about 400 units. Timeline: this quarter if possible.", "interested", requirements=("maintenance", "tickets"),
         timeline="this quarter", history=(OUR_FIRST,)),
    Case("conv-stop-late", "Actually, please stop emailing me. We went with another vendor.", "unsubscribe", stop=True,
         objection="competitor", history=(OUR_FIRST, "Maybe, send more details.", OUR_SECOND)),
    Case("conv-referral", "I've forwarded this to our CTO, Mark Chen. He'll reach out if it's a fit.", "referral",
         history=(OUR_FIRST,)),
    Case("conv-pricing", "Before a call, can you give me a ballpark price? We're comparing three vendors.", "question",
         objection="competitor", history=(OUR_FIRST, "Possibly interested.", OUR_SECOND)),
    Case("conv-meeting-yes", "Great, Tuesday at 10am works for me. Please send the invite.", "interested", meeting=True,
         history=(OUR_FIRST, "Happy to chat.", "Would Tuesday at 10am or Thursday at 3pm work?")),
    Case("conv-not-now", "Thanks for following up. We're not looking at this right now, maybe next year.", "objection",
         objection="timing", timeline="next year", history=(OUR_FIRST, OUR_SECOND)),
    Case("conv-human", "I'd rather speak with a person on your sales team, not an automated assistant.", "question",
         meeting=True, history=(OUR_FIRST, OUR_SECOND)),
    Case("conv-angry", "I never asked for any of this. Take me off your list or I will report you.", "complaint", stop=True,
         history=(OUR_FIRST, OUR_SECOND)),
]

CASES: list[Case] = [*_p6(), *SINGLE, *CONVERSATIONS]
