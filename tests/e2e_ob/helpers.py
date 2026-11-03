"""The Option B end-to-end harness: one synthetic US company driven through the real scheduler jobs (aime.workflows.JOBS
via aime.scheduler.runner.run_job) on an isolated database, with every outside service faked.

- the public web (Hacker News, the company's own website with a team page, SEC) is an httpx MockTransport that records
  every host asked for;
- the mail-server (MX) check is a table (MX), never DNS;
- SMTP and IMAP are fakes, installed as the default of every sender and mailbox the jobs build themselves;
- the conversation engine's model is absent (rules) or a scripted fake that only states what the email says;
- the Calendly notice is DKIM-signed with the test key of tests/test_dkim.py and verified against it.

`sweep(world)` is the invariant check run at the end of every scenario: no email without a person's approval, none to a
suppressed address, the sending limits as they were, no publishing, no real network."""

from __future__ import annotations

import copy
import csv
import imaplib
import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.timeutil
from aime import handover_notify as hn
from aime import handover_package as hp
from aime import manager_report as mr
from aime import settings_store as ss
from aime.contact_validation import MailDomainChecker
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.llm.base import LLMProvider
from aime.outreach import compliance_footer
from aime.models import (Company, Contact, ContentItem, FollowUp, HandoverPackage, InboundEmail, Lead, Opportunity,
                         Suppression)
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.conftest import hn_handler
from tests.test_company_research import SITE, make_handler
from tests.test_inbox import MAIL, FakeIMAP, mail

ORIGIN = {"origin": "http://testserver"}
DAVID = f"david.chen@{SITE}"
MANAGER_ADDR = "manager@us.example"  # the Sales Manager's internal address, on the sending mailbox's own domain
MX = {SITE: True}  # the domains that take mail; anything else: no mail server
PUBLISH_HOSTS = ("linkedin.com", "api.linkedin.com", "www.linkedin.com")


def at(when: datetime) -> datetime:
    """Move the code's clock (aime.timeutil.utcnow) to `when` (naive UTC); it keeps running from there."""
    aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
    return when


def forward(delta: timedelta) -> datetime:
    return at(utcnow() + delta)


# --------------------------------------------------------------------------------------------------- fake mail servers

class FakeSMTP:
    """Records every message it accepts. Switches: `fail_connect` (refused: certainly nothing left), `die_before_accept`
    (the process dies after the send was claimed, before the server took the message), `die_after_accept` (the server
    took it and the process died before recording it)."""

    sent: list = []
    connects = 0
    fail_connect = False
    die_before_accept = False
    die_after_accept = False

    @classmethod
    def reset(cls):
        cls.sent, cls.connects = [], 0
        cls.fail_connect = cls.die_before_accept = cls.die_after_accept = False

    def __init__(self, host, port, timeout=30):
        FakeSMTP.connects += 1
        if FakeSMTP.fail_connect:
            raise ConnectionRefusedError("connection refused (fake)")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def ehlo(self, *a):
        return 250, b"ok"

    def quit(self):
        pass

    def close(self):
        pass

    def send_message(self, msg):
        if FakeSMTP.die_before_accept:
            raise KeyboardInterrupt  # the process is killed while the message is being handed over
        FakeSMTP.sent.append(msg)
        if FakeSMTP.die_after_accept:
            raise KeyboardInterrupt


class OBIMAP(FakeIMAP):
    """tests.test_inbox.FakeIMAP with an outage switch: `down` refuses the connection."""

    down = False
    connects = 0

    def __init__(self, host, port, ssl_context=None, timeout=None):
        OBIMAP.connects += 1
        if OBIMAP.down:
            raise OSError("mail server unreachable (fake outage)")
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)


def install_fakes(monkeypatch) -> None:
    """Every sender and mailbox the jobs build for themselves gets the fake servers; the MX check reads the table."""
    FakeSMTP.reset()
    MX.clear()
    MX[SITE] = True
    OBIMAP.down, OBIMAP.connects = False, 0
    FakeIMAP.messages = {}
    monkeypatch.setattr(EmailSender.__init__, "__defaults__", (FakeSMTP, None))
    monkeypatch.setattr(Mailbox.__init__, "__defaults__", (OBIMAP, 14, 50))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", OBIMAP)
    for fn in (hn.deliver, hn.run, mr.deliver, mr.run):
        d = list(fn.__defaults__)
        d[d.index(next(x for x in d if getattr(x, "__name__", "") == "SMTP"))] = FakeSMTP
        monkeypatch.setattr(fn, "__defaults__", tuple(d))
    monkeypatch.setattr(MailDomainChecker, "_lookup", lambda self, domain: MX.get(domain, False))


# --------------------------------------------------------------------------------------------------- the model

class ScriptedLLM(LLMProvider):
    """A well-behaved model: for each email it states only facts whose quote is in that email (`script`: a phrase of
    the email -> the facts it reads there). `refuse` makes it unreachable, like Ollama refused or timing out."""

    name = "fake"

    def __init__(self, script: dict[str, list[dict]] | None = None, refuse: Exception | None = None):
        self.model = "scripted-1"
        self.script = dict(script or {})
        self.refuse = refuse
        self.calls = 0

    @property
    def label(self) -> str:
        return "fake:scripted"

    def available(self) -> bool:
        return self.refuse is None

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7) -> str:
        self.calls += 1
        if self.refuse is not None:
            raise self.refuse
        facts = [f for phrase, fs in self.script.items() if phrase in prompt for f in fs]
        return json.dumps({"facts": facts, "upset": False, "asks_for_human": False})


SCRIPT = {
    "supplier invoices": [{"kind": "requirement", "value": "automate supplier invoice entry",
                           "quote": "automate how we enter supplier invoices", "confidence": 0.9}],
    "$15k": [{"kind": "budget", "value": "about USD 15,000", "quote": "budget is about $15k", "confidence": 0.9},
             {"kind": "concern", "value": "data stays in the United States", "quote": "our data has to stay in the United States",
              "confidence": 0.8}],
    "by March": [{"kind": "timeline", "value": "live by March", "quote": "live by March", "confidence": 0.9}],
}


# --------------------------------------------------------------------------------------------------- the world

def recording(handler, hosts: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return handler(request)
    return handle


def site_and_hn(requests: list[str]):
    site = make_handler(requests)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host in (SITE, "data.sec.gov", "www.sec.gov"):
            return site(request)
        return hn_handler(request)
    return handle


class World:
    """One synthetic company (Brightline Logistics, brightline-logistics.example) and its decision-maker David Chen, run
    through the real jobs. `brain`: "rules" (no model) or "model" (ScriptedLLM)."""

    def __init__(self, make_ctx, monkeypatch, brain: str = "rules", data_dir=None, handler=None, **overrides):
        install_fakes(monkeypatch)
        self.make_ctx, self.monkeypatch, self.brain, self.overrides = make_ctx, monkeypatch, brain, overrides
        self.hosts: list[str] = []
        self.site_requests: list[str] = []
        self.handler = handler or site_and_hn(self.site_requests)
        self.ctx = self._context(data_dir)
        self.limits = self._limits()
        self.thread: str | None = None
        self.n = 0

    def _context(self, data_dir=None):
        kw = {**MAIL, "calendly_verified_only": True, **self.overrides}
        if data_dir is not None:
            kw["data_dir"] = data_dir
        ctx = self.make_ctx(handler=recording(self.handler, self.hosts), **kw)
        ctx.settings.business.setdefault("handover", {})["sales_manager"] = "manager"
        ctx.conversation_llm = ScriptedLLM(SCRIPT) if self.brain == "model" else None
        return ctx

    def restart(self):
        """The PC restarts: a new process (a new AppContext) on the same data folder and database."""
        old = self.ctx
        old.db.dispose()
        self.ctx = self._context(old.settings.data_dir)
        return self.ctx

    def _limits(self) -> dict:
        return {"outreach": copy.deepcopy(self.ctx.settings.section("outreach")),
                "deliverability": copy.deepcopy(self.ctx.settings.section("deliverability")),
                "deliverability_section": copy.deepcopy(ss.get(self.ctx.settings, "deliverability")),
                "sales_overrides": copy.deepcopy(ss.get(self.ctx.settings, "sales")),
                "approval_required": self.ctx.settings.require_human_approval}

    @property
    def client(self) -> TestClient:
        return TestClient(create_app(self.ctx))

    def job(self, name: str, expect: str | tuple = ("success",)) -> dict:
        res = run_job(self.ctx, name)
        expect = (expect,) if isinstance(expect, str) else expect
        assert res["status"] in expect, (name, res)
        return res

    # ---- the stages
    def conversation_mode(self, mode: str = "drafts", **extra):
        with self.ctx.db.session() as s:
            ss.save(s, self.ctx.settings, "conversation", {**ss.get(self.ctx.settings, "conversation"), "mode": mode, **extra}, "owner")

    def import_list(self):
        """The owner's own list (an event attendee CSV), the authorized discovery source of this scenario."""
        self.ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
        with (self.ctx.settings.imports_dir / "event-list.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "website", "state", "industry", "notes"])
            w.writerow(["Brightline Logistics", f"https://{SITE}", "OH", "saas",
                        "Dispatchers re-key orders from email into spreadsheets; they want to automate manual data entry "
                        "and replace spreadsheets with a CRM"])

    def lead(self) -> Lead:
        with self.ctx.db.session() as s:
            return s.execute(select(Lead).join(Company, Company.id == Lead.company_id).where(Company.domain == SITE)).scalar_one()

    def followups(self, **where) -> list[FollowUp]:
        with self.ctx.db.session() as s:
            q = select(FollowUp).where(FollowUp.channel == "email").order_by(FollowUp.id)
            for k, v in where.items():
                q = q.where(getattr(FollowUp, k) == v)
            return s.execute(q).scalars().all()

    def followup(self, fid: int) -> FollowUp:
        with self.ctx.db.session() as s:
            return s.get(FollowUp, fid)

    def approve(self, fid: int):
        r = self.client.post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303, r.text[:300]

    def send(self, expect=("success",)) -> dict:
        """A check-in: read the mailbox, then the one job that sends."""
        self.job("inbox")
        return self.job("followups", expect)

    def say(self, text: str, frm: str | None = None, msg_id: str | None = None) -> InboundEmail:
        """The prospect writes in the thread; the inbox job reads it."""
        self.n += 1
        uid = max(FakeIMAP.messages, default=0) + 1
        FakeIMAP.messages[uid] = mail(frm or f"David Chen <{DAVID}>", "Re: our note", text, in_reply_to=self.thread,
                                      msg_id=msg_id or f"<turn{self.n}@{SITE}>")
        self.job("inbox")
        with self.ctx.db.session() as s:
            return s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()

    def answer(self, row: InboundEmail) -> FollowUp:
        """A person approves the suggested answer; the followups job sends it; the thread moves on."""
        assert row.suggested_followup_id, (row.category, row.status)
        before = len(FakeSMTP.sent)
        self.approve(row.suggested_followup_id)
        forward(timedelta(minutes=5))
        self.send()
        f = self.followup(row.suggested_followup_id)
        assert f.status == "sent" and len(FakeSMTP.sent) == before + 1, (f.status, f.blocked_reason)
        self.thread = f.message_id
        return f

    def calendly(self, raw: bytes, monkeypatch, records) -> InboundEmail:
        from aime import dkim_verify as dv
        from tests.test_dkim import _key
        monkeypatch.setattr(dv, "_public_key", lambda selector, domain, resolver=None: _key(records, selector, domain))
        FakeIMAP.messages[max(FakeIMAP.messages, default=0) + 1] = raw
        self.job("inbox")
        with self.ctx.db.session() as s:
            return s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()

    def authorize_handover_email(self):
        with self.ctx.db.session() as s:
            hn.save(s, self.ctx.settings, "owner", {"addresses": f"manager = {MANAGER_ADDR}", "email_enabled": True})
            hn.authorize(s, self.ctx.settings, "owner")

    def opportunity(self) -> Opportunity:
        with self.ctx.db.session() as s:
            return s.execute(select(Opportunity)).scalar_one()

    def package(self) -> HandoverPackage | None:
        with self.ctx.db.session() as s:
            opp = s.execute(select(Opportunity)).scalars().first()
            return hp.latest(s, opp.id) if opp else None


# --------------------------------------------------------------------------------------------------- shared steps

OTHER = "pat.lee@harbor-works.example"


def sends_to(address: str) -> int:
    return sum(1 for m in FakeSMTP.sent if address in (m["To"] or ""))


def drafted_and_approved(w: World) -> FollowUp:
    """Discovery to an approved first email, by the real jobs, without sending it."""
    w.import_list()
    for job in ("plan", "research", "enrich", "research"):
        w.job(job)
    (draft,) = w.followups(kind="outreach", to_email=DAVID)
    w.approve(draft.id)
    return w.followup(draft.id)


def second_prospect(w: World) -> int:
    """Another company's decision-maker, a published address on its own domain, with a first email a person approves."""
    MX["harbor-works.example"] = True
    with w.ctx.db.session() as s:
        co = Company(name="Harbor Works", normalized_name="harbor works", domain="harbor-works.example",
                     website="https://harbor-works.example", website_status="verified", source="csv_import", state="TX")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, first_name="Pat", last_name="Lee", title="Chief Operating Officer", email=OTHER,
                     source="website", consent_status="none", email_status="published",
                     email_source_url="https://harbor-works.example/team", evidence="Pat Lee, Chief Operating Officer",
                     evidence_url="https://harbor-works.example/team", discovered_at=utcnow())
        s.add(ct)
        s.flush()
        ld = Lead(company_id=co.id, contact_id=ct.id, source="research", status="qualified", grade="A", score=85)
        s.add(ld)
        s.flush()
        f = FollowUp(lead_id=ld.id, contact_id=ct.id, company_id=co.id, channel="email", kind="outreach", step=1, to_email=OTHER,
                     status="awaiting_approval", subject="Order entry at Harbor Works",
                     body="Hi Pat,\n\nA short note about order entry. Worth a talk?" + compliance_footer(w.ctx.settings),
                     due_at=utcnow() - timedelta(minutes=5))
        s.add(f)
        s.flush()
        fid = f.id
    w.approve(fid)
    return fid


# --------------------------------------------------------------------------------------------------- the invariants

def _addr(header: str | None) -> str:
    from email.utils import parseaddr
    return parseaddr(header or "")[1].lower()


def sweep(w: World, network_attempts: list) -> None:
    """What must hold after every scenario, whatever happened in it."""
    with w.ctx.db.session() as s:
        fus = s.execute(select(FollowUp)).scalars().all()
        supp = s.execute(select(Suppression)).scalars().all()
        pkgs = s.execute(select(HandoverPackage)).scalars().all()
        # 1. nothing went out (or may have) without a person's approval
        for f in fus:
            if f.status in ("sent", "sending", "uncertain") or f.sent_at:
                assert f.approved_at is not None and f.approved_by, (f.id, f.status, f.approved_by)
        sent_by_mid = {f.message_id: f for f in fus if f.message_id}
        internal = {p.notify_to.lower() for p in pkgs if p.notify_to}
        internal |= {MANAGER_ADDR}
        # 2. every message the fake server accepted is an approved follow-up, or the internal handover/report email
        for msg in FakeSMTP.sent:
            to = _addr(msg["To"])
            if to in internal:
                continue
            f = sent_by_mid.get(msg["Message-ID"])
            assert f is not None and f.approved_at is not None, ("an email no approved follow-up accounts for", to, msg["Subject"])
            assert f.approved_at <= (f.sent_at or utcnow()), (f.id, "sent before it was approved")
            assert to.endswith(".example"), ("a non-synthetic recipient", to)
            # 3. never to an address (or domain) already on the do-not-contact list when it went
            for sp in supp:
                v = sp.value.lower()
                hit = v == to if sp.kind == "email" else to.endswith("@" + v)
                if hit and f.sent_at is not None:
                    assert f.sent_at < sp.created_at, ("sent to a suppressed address", to, f.id)
        # 4. publishing never attempted
        assert all(c.status != "published" for c in s.execute(select(ContentItem)).scalars())
    assert not w.ctx.settings.publishing_enabled
    from aime.publish_journal import journal_path
    pj = journal_path(w.ctx.settings.data_dir)
    assert not pj.exists() or not pj.read_text(encoding="utf-8").strip(), "a publish was recorded"
    assert not any(h.endswith(PUBLISH_HOSTS) for h in w.hosts), [h for h in w.hosts if "linkedin" in h]
    # 5. the sending limits are what they were
    assert w._limits() == w.limits
    # 6. no real network: every connection the code tried went to a fake
    assert network_attempts == [], network_attempts
