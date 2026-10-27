"""The whole supervised sales workflow for one US company, in controlled test mode, offline:
authorized discovery -> website check -> research -> decision-maker -> published email -> score -> CRM ->
evidence-quoted draft -> approval (nothing before it) -> test-mode send -> follow-up step -> reply from the
owner's test address in the thread -> classification -> sequence stopped -> suggested reply sent ->
Calendly booking -> meeting -> report -> acceptance evidence."""

import csv
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.acceptance import acceptance_evidence
from aime.agents.analytics import AnalyticsReportingAgent
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.inbox_agent import InboxAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.models import Company, FollowUp, InboundEmail, Lead, Meeting
from aime.scheduler.runner import run_job
from aime.web.app import create_app
from tests.conftest import hn_handler
from tests.test_company_research import SITE, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, FakeIMAP, mail
from tests.test_meetings_pipeline import BOOKED_BODY

ORIGIN = {"origin": "http://testserver"}
OWNER = "owner@owner-test.example"


def handler_for(requests):
    site = make_handler(requests)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host in (SITE, "data.sec.gov", "www.sec.gov"):
            return site(request)
        return hn_handler(request)
    return handle


def test_one_company_through_every_stage(make_ctx):
    ctx = make_ctx(handler=handler_for([]), outbound_test_recipients=OWNER, **MAIL)
    FakeSMTP.sent, FakeIMAP.messages = [], {}
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    with (ctx.settings.imports_dir / "event-list.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "website", "state", "industry", "notes"])
        w.writerow(["Brightline Logistics", f"https://{SITE}", "OH", "saas",
                    "Dispatchers re-key orders from email into spreadsheets; they want to automate manual data entry "
                    "and replace spreadsheets with a CRM"])

    # 1-2: authorized discovery, website check, qualification (real jobs)
    assert run_job(ctx, "plan")["status"] == "success"
    assert run_job(ctx, "research")["status"] == "success"
    with ctx.db.session() as s:
        company = s.execute(select(Company).where(Company.domain == SITE)).scalar_one()
        assert company.source == "csv_import" and company.website_status == "verified"
        cid = company.id
    # 2-6: research, decision-maker, published email, score, CRM
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    with ctx.db.session() as s:
        lead = s.execute(select(Lead).where(Lead.company_id == cid)).scalar_one()
        assert lead.status == "qualified" and lead.grade in ("A", "B"), (lead.score, lead.score_breakdown)
        assert lead.contact.display_name == "David Chen" and lead.contact.email_status == "published"
        lead_id = lead.id
    # 7-8: draft; nothing is sent before approval
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        draft = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.kind == "outreach")).scalar_one()
    client = TestClient(create_app(ctx))
    client.post(f"/followups/{draft.id}/approve", headers=ORIGIN)
    # 9: controlled send: delivered only to the owner's test address
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == OWNER
    assert FakeSMTP.sent[0]["Subject"].startswith(f"[TEST for david.chen@{SITE}]")
    with ctx.db.session() as s:
        sent = s.get(FollowUp, draft.id)
        assert sent.status == "sent" and sent.delivered_to == OWNER
    # 10: follow-up step (due after 4 days; the following Monday, inside the weekday send window), once
    later = TUESDAY_11AM_ET + timedelta(days=6)
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()}).output.outreach_drafts == 1
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()}).output.outreach_drafts == 0
    # 11-12: the owner replies in the thread (a consented, controlled reply)
    FakeIMAP.messages = {1: mail(f"Owner Test <{OWNER}>", f"Re: {FakeSMTP.sent[0]['Subject']}",
                                 "Sounds interesting. Could we talk Thursday?", in_reply_to=sent.message_id)}
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({}).ok
    with ctx.db.session() as s:
        reply = s.execute(select(InboundEmail)).scalar_one()
        assert reply.category == "interested" and reply.lead_id == lead_id and not reply.is_simulated
        step2 = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalar_one()
        assert step2.status == "cancelled"
        suggestion = s.get(FollowUp, reply.suggested_followup_id)
    client.post(f"/followups/{suggestion.id}/approve", headers=ORIGIN)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (later + timedelta(minutes=30)).isoformat()})
    assert FakeSMTP.sent[-1]["To"] == OWNER and "calendly.com" in FakeSMTP.sent[-1].get_content()
    # 14: the owner books through Calendly; the booking is matched to the lead by the thread's address
    FakeIMAP.messages[2] = mail("Calendly <notifications@calendly.com>",
                                "New Event: Owner Test - 10:00am Thu, Oct 29, 2026 - 20 Minute Meeting",
                                BOOKED_BODY.replace("David Chen", "Owner Test").replace(f"david.chen@{SITE}", OWNER))
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({}).ok
    with ctx.db.session() as s:
        meeting = s.execute(select(Meeting)).scalar_one()
        assert meeting.lead_id == lead_id and s.get(Lead, lead_id).outreach_status == "meeting"
    # reporting from records
    k = AnalyticsReportingAgent(ctx).run({"kind": "daily"}).output.kpis
    assert (k["outreach_sent_total"], k["replies_total"], k["interested_total"], k["meetings_booked_total"]) == (1, 1, 1, 1)
    # acceptance evidence
    with ctx.db.session() as s:
        rows = {r["test"]: r for r in acceptance_evidence(s, ctx.settings)}
    for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 14):
        assert rows[n]["status"].startswith("PASS"), rows[n]
    assert rows[9]["status"] == "PASS (test mode)" and rows[19]["status"].startswith("PASS (test mode)")
    assert rows[18]["status"] == "SEE TEST RUN" and rows[15]["status"] == "CHECK ON SCREEN"


def test_alternate_settings_file_is_isolated_and_guarded(tmp_path, monkeypatch, capsys):
    from aime.cli import PRODUCTION_DATA_DIR, main
    from aime.config import Settings

    for name in ("AIME_DATA_DIR", "AIME_DATA_ROLE", "OUTBOUND_EMAIL_ENABLED", "OUTBOUND_TEST_RECIPIENTS"):
        monkeypatch.delenv(name, raising=False)  # real environment variables outrank any settings file (CI sets some)
    acceptance = tmp_path / "acceptance.env"
    acceptance.write_text(f"AIME_DATA_DIR={tmp_path / 'acc-data'}\nOUTBOUND_TEST_RECIPIENTS={OWNER}\n", encoding="utf-8")
    monkeypatch.setenv("AIME_ENV_FILE", str(acceptance))
    s = Settings.load()
    assert s.data_dir == tmp_path / "acc-data" and s.test_recipients == {OWNER} and not s.outbound_email_enabled
    bad = tmp_path / "bad.env"
    bad.write_text(f"AIME_DATA_DIR={PRODUCTION_DATA_DIR}\n", encoding="utf-8")
    monkeypatch.setenv("AIME_ENV_FILE", str(bad))
    assert main(["status"]) == 2 and "Nothing was run" in capsys.readouterr().out


def test_simulated_replies_are_refused_on_live_production_data(make_ctx, monkeypatch, capsys):
    from argparse import Namespace

    from aime.cli import cmd_simulate_reply
    ctx = make_ctx(data_role="production")
    monkeypatch.delenv("AIME_ENV_FILE", raising=False)
    assert cmd_simulate_reply(Namespace(lead=1, text="hi", from_email=None), ctx) == 2
    assert "refused" in capsys.readouterr().out


def test_cli_output_survives_a_console_that_cannot_show_arrows(make_ctx, capsys, monkeypatch):
    import io
    import sys

    from aime.cli import _console_safe
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\n")  # a console code page without "→"
    monkeypatch.setattr(sys, "stdout", narrow)
    _console_safe()
    print("discovered → researched → meeting")
    sys.stdout.flush()
    assert narrow.buffer.getvalue().decode("cp1252") == "discovered ? researched ? meeting\n"
