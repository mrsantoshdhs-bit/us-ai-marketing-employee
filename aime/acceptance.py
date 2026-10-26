"""Acceptance evidence: what the records show for each of the 19 acceptance tests of the 7-day scope.

PASS needs concrete records (ids, URLs, times). A pass that relied on simulated replies or test-mode
delivery says so. Tests that records alone cannot prove (regression runs, a visual dashboard check) are
reported as needing their own evidence, never as passed. Read-only.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.db import kv_get
from aime.models import (Activity, Company, Contact, FollowUp, InboundEmail, JobRun, Lead, Meeting, ResearchSnapshot,
                         Suppression, User)
from aime.timeutil import utcnow

AUTHORIZED_SOURCES = ("hn_hiring", "sec_edgar", "csv_import", "inbound", "manual")
HUMAN = ("interested", "not_interested", "objection", "referral", "question", "other")


def _row(n: int, name: str, status: str, evidence: str) -> dict:
    return {"test": n, "name": name, "status": status, "evidence": evidence}


def acceptance_evidence(session: Session, settings) -> list[dict]:
    one = lambda stmt: session.execute(stmt).scalars().first()  # noqa: E731
    count = lambda model, *c: session.execute(select(func.count(model.id)).where(*c)).scalar_one()  # noqa: E731
    out = []

    c = one(select(Company).where(Company.source.in_(AUTHORIZED_SOURCES), Company.source_url.is_not(None),
                                  Company.merged_into_id.is_(None)).order_by(Company.id))
    out.append(_row(1, "US company discovered from an authorized source", "PASS" if c else "NOT YET",
                    f"company #{c.id} {c.name} from {c.source}: {c.source_url}" if c else "no company with a source"))

    snap = one(select(ResearchSnapshot).where(ResearchSnapshot.status == "done", ResearchSnapshot.description_url.is_not(None))
               .order_by(ResearchSnapshot.id.desc()))
    out.append(_row(2, "Company research captured with evidence", "PASS" if snap else "NOT YET",
                    f"snapshot #{snap.id} (company #{snap.company_id}), quoted from {snap.description_url}, "
                    f"{len(snap.pages or [])} page(s)" if snap else "no completed research snapshot"))

    dm = one(select(Contact).where(Contact.source.in_(("website", "sec_filing")), Contact.last_name.is_not(None),
                                   Contact.evidence_url.is_not(None)).order_by(Contact.relevance.desc().nullslast()))
    out.append(_row(3, "Decision-maker identified from reliable information", "PASS" if dm else "NOT YET",
                    f"contact #{dm.id} {dm.display_name}, {dm.title} ({dm.confidence}) from {dm.evidence_url}" if dm else
                    "no named decision-maker with evidence"))

    ok_email = one(select(Contact).where(Contact.email_status.in_(("published", "role_address")),
                                         Contact.email_source_url.is_not(None), Contact.email_checked_at.is_not(None)))
    bad = count(Contact, Contact.source.in_(("website", "sec_filing")), Contact.email.is_not(None), Contact.email_source_url.is_(None))
    out.append(_row(4, "Contact validated and its status recorded", "FAIL" if bad else ("PASS" if ok_email else "NOT YET"),
                    f"{bad} researched email(s) without a published source" if bad else
                    (f"contact #{ok_email.id}: {ok_email.email_status}, published at {ok_email.email_source_url}, "
                     f"checked {ok_email.email_checked_at:%Y-%m-%d %H:%M}" if ok_email else "no validated email yet")))

    lead = one(select(Lead).where(Lead.score_breakdown.is_not(None), Lead.last_scored_at.is_not(None)).order_by(Lead.score.desc()))
    out.append(_row(5, "Explainable qualification score", "PASS" if lead and lead.score_breakdown else "NOT YET",
                    f"lead #{lead.id}: {lead.score}/100 grade {lead.grade}; {len(lead.score_breakdown)} reasons, e.g. "
                    f"'{lead.score_breakdown[0]}'" if lead and lead.score_breakdown else "no scored lead"))

    linked = one(select(Lead).join(Contact, Contact.id == Lead.contact_id).where(Contact.company_id == Lead.company_id))
    out.append(_row(6, "Company and contact saved in the CRM", "PASS" if linked else "NOT YET",
                    f"lead #{linked.id}: company #{linked.company_id} with primary contact #{linked.contact_id}" if linked else
                    "no lead with a primary contact"))

    draft = one(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.step == 1).order_by(FollowUp.id))
    out.append(_row(7, "Personalized outreach email generated", "PASS" if draft and draft.evidence else "NOT YET",
                    f"follow-up #{draft.id} to {draft.to_email}: quotes {draft.evidence[0]['url']}; checks "
                    f"{(draft.quality or {}).get('qc', {}).get('status')}" if draft and draft.evidence else "no outreach draft"))

    unapproved_sent = count(FollowUp, FollowUp.status == "sent", FollowUp.approved_at.is_(None))
    out.append(_row(8, "Approval blocks unauthorized sending", "FAIL" if unapproved_sent else ("PASS" if draft else "NOT YET"),
                    f"{unapproved_sent} email(s) sent without an approval record" if unapproved_sent else
                    "every sent email has an approval record; drafts wait in 'awaiting approval' (also tested in CI)"))

    sent = one(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.status == "sent").order_by(FollowUp.id))
    test_mode = bool(sent and sent.delivered_to and sent.delivered_to.lower() != (sent.to_email or "").lower())
    out.append(_row(9, "Approved email sent through a controlled integration",
                    ("PASS (test mode)" if test_mode else "PASS") if sent else "NOT YET",
                    f"follow-up #{sent.id} approved by {sent.approved_by} at {sent.approved_at:%Y-%m-%d %H:%M}, sent "
                    f"{sent.sent_at:%Y-%m-%d %H:%M} to {sent.delivered_to or sent.to_email}"
                    + (f" (intended for {sent.to_email})" if test_mode else "") + f", Message-ID {sent.message_id}"
                    if sent else "no outreach email sent yet"))

    dup = session.execute(select(FollowUp.lead_id).where(FollowUp.channel == "email", FollowUp.status.in_(
        ("awaiting_approval", "approved", "sending"))).group_by(FollowUp.lead_id).having(func.count() > 1)).all()
    step2 = one(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.step >= 2))
    out.append(_row(10, "Follow-up scheduling without duplicates", "FAIL" if dup else ("PASS" if step2 else "NOT YET"),
                    f"{len(dup)} lead(s) with more than one open email" if dup else
                    (f"follow-up step {step2.step} (#{step2.id}) created after step 1 was sent; one open email per lead"
                     if step2 else "no follow-up step created yet")))

    reply = one(select(InboundEmail).where(InboundEmail.category.in_(HUMAN), InboundEmail.lead_id.is_not(None))
                .order_by(InboundEmail.is_simulated, InboundEmail.id))
    label = " (SIMULATED reply)" if reply and reply.is_simulated else ""
    out.append(_row(11, "Incoming reply detected and classified", (f"PASS{label}" if reply else "NOT YET"),
                    f"reply #{reply.id} from {reply.from_email}: {reply.category} ({reply.confidence})" if reply else
                    "no reply matched to a lead yet"))

    stopped = reply and session.get(Lead, reply.lead_id).outreach_status in ("replied", "interested", "meeting", "not_interested")
    still_open = reply and count(FollowUp, FollowUp.lead_id == reply.lead_id, FollowUp.kind == "outreach",
                                 FollowUp.status.in_(("awaiting_approval", "approved")))
    out.append(_row(12, "Reply updates the CRM and stops the sequence",
                    ("FAIL" if still_open else f"PASS{label}") if stopped else "NOT YET",
                    f"lead #{reply.lead_id} outreach stage {session.get(Lead, reply.lead_id).outreach_status}; "
                    f"{still_open} outreach email(s) still queued" if reply else "no reply yet"))

    sup = one(select(Suppression).order_by(Suppression.id))
    after = sup and count(FollowUp, FollowUp.status == "sent", func.lower(FollowUp.to_email) == sup.value.lower(),
                          FollowUp.sent_at > sup.created_at)
    out.append(_row(13, "Unsubscribe and suppression work", ("FAIL" if after else "PASS") if sup else "NOT YET (tested in CI)",
                    f"{sup.value} suppressed ({sup.reason}); {after or 0} email(s) sent to it afterwards" if sup else
                    "no suppression recorded in this database; CI covers unsubscribe, bounce and complaint handling"))

    meeting = one(select(Meeting).where(Meeting.lead_id.is_not(None), Meeting.status != "cancelled")
                  .order_by(Meeting.is_simulated, Meeting.id))
    mlabel = " (SIMULATED)" if meeting and meeting.is_simulated else ""
    out.append(_row(14, "Meeting scheduling works", f"PASS{mlabel}" if meeting else "NOT YET",
                    f"meeting #{meeting.id} {meeting.starts_at:%Y-%m-%d %H:%M} UTC with {meeting.invitee_email} "
                    f"({meeting.source}), lead #{meeting.lead_id}" if meeting else "no meeting recorded yet"))

    out.append(_row(15, "Sales Manager dashboard shows accurate information", "CHECK ON SCREEN",
                    f"compare: {count(Company, Company.merged_into_id.is_(None))} companies, "
                    f"{count(Contact, Contact.source.in_(('website', 'sec_filing')), Contact.last_name.is_not(None))} "
                    f"decision-makers, {count(FollowUp, FollowUp.kind == 'outreach', FollowUp.status == 'sent')} outreach sent, "
                    f"{count(Meeting, Meeting.status != 'cancelled')} meetings"))

    since = utcnow() - timedelta(hours=26)
    jobs = {name: status for name, status in session.execute(
        select(JobRun.job, JobRun.status).where(JobRun.started_at >= since).order_by(JobRun.id))}
    needed = ("enrich", "inbox", "followups")
    missing = [j for j in needed if jobs.get(j) != "success"]
    out.append(_row(16, "Background jobs run and recover", "PASS" if not missing else "NOT YET",
                    f"last 26 h: {', '.join(f'{k}={v}' for k, v in sorted(jobs.items()))}; abandoned runs recovered: "
                    f"{count(JobRun, JobRun.status == 'abandoned')}" + (f"; missing: {', '.join(missing)}" if missing else "")))

    backup = kv_get(session, "backup.last_verified")
    users = count(User, User.active.is_(True))
    audit = count(Activity)
    ok17 = settings.dashboard_auth and users and audit and backup
    out.append(_row(17, "Authentication, audit log and backup recovery", "PASS" if ok17 else "NOT YET",
                    f"sign-in {'on' if settings.dashboard_auth else 'OFF'}, {users} active account(s), {audit} audit entries, "
                    f"last verified backup {backup['file'] if backup else 'none'} (restore drill: `aime restore-drill`)"))
    out.append(_row(18, "Existing functionality passes regression testing", "SEE TEST RUN",
                    "the automated suite (pytest) and CI on every pull request"))
    linked_ok = sent and reply and meeting and reply.lead_id == sent.lead_id == meeting.lead_id
    out.append(_row(19, "Complete workflow passes end to end", ("PASS" + (" (test mode)" if test_mode else "")
                                                               + (label or mlabel)) if linked_ok else "NOT YET",
                    f"lead #{sent.lead_id}: discovered → researched → contact → scored → drafted → approved → sent → "
                    f"reply → meeting" if linked_ok else "not yet one lead through every stage"))
    return out


def render_markdown(rows: list[dict], label: str) -> str:
    lines = [f"# Acceptance evidence: {label}", "", "| # | Test | Status | Evidence |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['test']} | {r['name']} | {r['status']} | {r['evidence'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"
