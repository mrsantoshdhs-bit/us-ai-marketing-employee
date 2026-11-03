"""US Business Research Agent: discovers US companies from authorized public sources and records evidence."""

from __future__ import annotations

from collections import Counter, defaultdict

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime import countries
from aime.agents.base import BaseAgent, TransientAgentError
from aime.agents.contracts import SourceReport
from aime.agents.qualification import NOT_A_PERSON
from aime.contact_validation import STATUS_OK
from aime.crm_ops import (CONTACT_NOT_SUPPRESSED, OPEN_LEAD_STATUSES, add_signal, ensure_lead, is_suppressed, log_activity,
                          upsert_company)
from aime.dedupe import find_duplicate
from aime.matching import competitor_in_post
from aime.models import Activity, Alert, Company, Contact, Lead, Opportunity, Signal
from aime.monitoring import alert_once, resolve_alerts
from aime.outreach import _merged_family, company_opted_out
from aime.pipeline import _task
from aime.redact import redact
from aime.sources.base import ResearchFocus
from aime.sources.registry import ordered_sources
from aime.validation import validate_candidate

EVIDENCE_ONLY_SOURCES = ("sec_edgar",)  # the owner's decision of 1 Nov 2026: a filing is market evidence and makes no lead
ON_THE_LIST_REASON = "suppression list"  # how scoring words the close of a lead on the do-not-contact list (aime.scoring)
STALE_ALERT_HOURS = 48  # a source living on its saved answer for longer than a missed day or two: a person is told


def _leads_on_file(session: Session, company: Company) -> list[Lead]:
    return session.execute(select(Lead).where(Lead.company_id.in_(_merged_family(session, company)), Lead.status != "merged")
                           .order_by(Lead.id)).scalars().all()


def _closed_by_person(session: Session, lead: Lead) -> bool:
    """A person set its status last. A close with no record of who did it counts as theirs."""
    last = session.execute(select(Activity.actor).where(Activity.lead_id == lead.id, Activity.type == "status_change")
                           .order_by(Activity.created_at.desc(), Activity.id.desc()).limit(1)).first()
    return last is None or (last[0] or "") not in NOT_A_PERSON


def _only_a_dead_address(session: Session, company: Company, leads: list[Lead]) -> bool:
    """Every lead closed by itself because its address bounced, and someone else at the company can still be written to. A bounce
    is nobody's no, so the company is not closed. With nobody else, a new lead would only close again the same way."""
    def bounced(lead: Lead) -> bool:
        return (ON_THE_LIST_REASON in (lead.disqualify_reason or "") and not _closed_by_person(session, lead)
                and (lead.outreach_status == "bounced" or (lead.contact is not None and lead.contact.email_status == "bounced")))
    return all(bounced(x) for x in leads) and session.execute(select(Contact.id).where(
        Contact.company_id.in_(_merged_family(session, company)), Contact.email_status.in_(STATUS_OK), CONTACT_NOT_SUPPRESSED).limit(1)).first() is not None


def _stays_closed(session: Session, company: Company, leads: list[Lead]) -> bool:
    """A company a person closed, a won customer, or one where someone opted out stays closed until a person reopens it (the owner's
    decision of 1 Nov 2026), and so does one AIME closed. An open lead is worked as before, and an inquiry is answered through its own path."""
    if any(x.status in OPEN_LEAD_STATUSES for x in leads):
        return False
    if company_opted_out(session, company):
        return True
    return bool(leads) and not _only_a_dead_address(session, company, leads)


class ResearchInput(BaseModel):
    industries: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    sources: list[str] | None = None
    limit: int | None = Field(default=None, ge=1, le=500)


class ResearchOutput(BaseModel):
    candidates_seen: int = 0
    non_us_skipped: int = 0
    unknown_location_skipped: int = 0
    competitors_flagged: int = 0
    invalid_skipped: int = 0
    invalid_reasons: dict[str, int] = Field(default_factory=dict)
    fixes_applied: int = 0
    companies_created: int = 0
    companies_matched_existing: int = 0
    signals_added: int = 0
    leads_created: int = 0
    limit_reached: bool = False
    new_company_ids: list[int] = Field(default_factory=list)
    sources: list[SourceReport] = Field(default_factory=list)
    import_files_archived: int = 0
    import_files_rejected: list[str] = Field(default_factory=list)
    import_notices: list[str] = Field(default_factory=list)  # files imported, but with rows left out
    sec_kept_as_evidence: int = 0  # companies known only from a filing: evidence on file, no lead


class USBusinessResearchAgent(BaseAgent[ResearchInput, ResearchOutput]):
    name = "research"
    title = "US Business Research Agent"
    responsibilities = [
        "Query enabled, authorized public sources (HN hiring API, SEC EDGAR, your CSV imports)",
        "Keep only companies with explicit US location evidence; reject malformed records",
        "Extract stated requirements and map them to our nine services",
        "Deduplicate against the CRM and store companies, evidence signals, and research leads",
        "Never store personal contact data from public posts and never infer buying intent",
    ]
    input_model = ResearchInput
    output_model = ResearchOutput

    def execute(self, inp: ResearchInput, session: Session) -> ResearchOutput:
        limit = inp.limit or int(self.settings.section("research").get("max_new_companies_per_run", 40))
        focus = ResearchFocus(industries=inp.industries, states=inp.states, limit=limit)
        out = ResearchOutput()
        candidates = []
        # Your own CSV lists are always "configured", so only the public sources tell whether discovery failed.
        public_sources, public_failures = 0, 0
        file_sources = []  # sources whose files are archived only after this run commits
        stale_alerts = []

        # Discovery follows the enabled target countries (aime.countries); with no saved country settings that is the
        # sales settings' Target country, as before. Each source is used for the countries it covers, by priority.
        wanted = countries.discovery_countries(self.settings)
        targets = countries.target_countries(self.settings)
        country = "/".join(wanted) or "no enabled"
        for src, conf in ordered_sources(self.ctx, inp.sources):
            covers = conf["countries"]
            if not conf["enabled"]:
                out.sources.append(SourceReport(name=src.name, configured=False, ok=False,
                                                message="switched off in Settings › Lead sources"))
                continue
            if "*" not in covers and not set(wanted) & set(covers):
                out.sources.append(SourceReport(name=src.name, configured=False, ok=False,
                                                message=f"no {country} coverage: this source finds {', '.join(covers)} companies"))
                continue
            ok, msg = src.configured()
            if not ok:
                out.sources.append(SourceReport(name=src.name, configured=False, ok=False, message=msg))
                continue
            is_public = not hasattr(src, "finish_intake")
            public_sources += int(is_public)
            problem = None
            try:
                found = src.fetch(focus)
            except httpx.HTTPStatusError as exc:
                problem = f"HTTP {exc.response.status_code}"
            except httpx.HTTPError as exc:
                problem = f"network: {exc}"
            except (ValueError, KeyError, TypeError, AttributeError) as exc:  # a 200 that is not what the API documents
                problem = "unexpected answer (" + redact(f"{type(exc).__name__}: {exc}")[:200] + ")"
            if problem:  # one source's failure must not stop the others
                public_failures += int(is_public)
                out.sources.append(SourceReport(name=src.name, configured=True, ok=False, message=problem))
                continue
            stale_hours = getattr(src, "stale_hours", None)
            if stale_hours is not None:  # still used: an old list beats none, but it must not read as a fresh answer
                msg = f"stale: the API gave no answer, using a saved one {stale_hours / 24:.1f} days old"
                if stale_hours > STALE_ALERT_HOURS:
                    stale_alerts.append(f"{src.name} ({msg})")
            if getattr(src, "unreadable_feeds", 0):
                msg = f"{msg}; {src.unreadable_feeds} state and industry pair(s) could not be read and were skipped"
            out.sources.append(SourceReport(name=src.name, configured=True, ok=True, records=len(found), message=msg))
            candidates.extend(found)
            if hasattr(src, "finish_intake"):
                file_sources.append(src)

        # Focus states/industries first so the per-run limit is spent where the plan points.
        focus_states, focus_inds = set(inp.states), set(inp.industries)
        candidates.sort(key=lambda c: (c.state not in focus_states, c.industry not in focus_inds))

        rows_left_out: dict[str, Counter] = defaultdict(Counter)  # per imported file, so the person is told

        def left_out(cand, why: str) -> None:
            if cand.extra.get("import_file"):
                rows_left_out[cand.extra["import_file"]][why] += 1

        public_created = 0  # the per-run cap applies to public-source discovery, not to your own CSV lists
        kept_as_evidence: set[int] = set()
        us_only = targets == ["US"]
        for cand in candidates:
            out.candidates_seen += 1
            where = countries.candidate_country(cand)
            if where is None:
                out.unknown_location_skipped += 1
                left_out(cand, "without a US state (add a state column)" if us_only
                         else "without a US state or a country (add a state or country column)")
                continue
            if where not in targets:
                out.non_us_skipped += 1
                left_out(cand, "outside the US" if us_only else "outside the enabled target countries")
                continue
            cand.country = where
            problems, fixes = validate_candidate(cand)
            if problems:
                out.invalid_skipped += 1
                for p in problems:
                    out.invalid_reasons[p] = out.invalid_reasons.get(p, 0) + 1
                left_out(cand, f"invalid: {problems[0]}")
                continue
            out.fixes_applied += len(fixes)
            user_list = cand.source == "csv_import"
            if public_created >= limit and not user_list:
                out.limit_reached = True
                # Still attach evidence to companies we already track.
                existing = find_duplicate(session, cand.name, cand.domain, cand.state, cand.source_record_id,
                                          cand.source)
                sig = existing and add_signal(session, existing, cand.signal_type, cand.signal_text, cand.source,
                                              cand.source_url, cand.observed_at)
                if sig:
                    out.signals_added += 1
                    leads = _leads_on_file(session, existing)
                    if cand.source not in EVIDENCE_ONLY_SOURCES and not existing.is_competitor and _stays_closed(session, existing, leads):
                        self._keep_closed(session, existing, leads, sig, cand, out)  # read once only: no later run hears of it
                continue
            company, created = upsert_company(session, cand)
            if created:
                out.companies_created += 1
                public_created += int(not user_list)
                out.new_company_ids.append(company.id)
                log_activity(session, f"Discovered via {cand.source}", self.name, type_="research",
                             body=cand.source_url, company_id=company.id)
            else:
                out.companies_matched_existing += 1
            if company.is_competitor:
                out.competitors_flagged += 1
            sig = add_signal(session, company, cand.signal_type, cand.signal_text, cand.source, cand.source_url,
                             cand.observed_at)
            if sig:
                out.signals_added += 1
            leads = _leads_on_file(session, company)
            if cand.source in EVIDENCE_ONLY_SOURCES:  # a lead already on file, of any source, stays as it is and keeps being worked
                if not leads:
                    kept_as_evidence.add(company.id)
            elif company.is_competitor:
                if not leads:  # scoring closes it with a reason, and its lead page offers Not a competitor
                    lead, _ = ensure_lead(session, company, "research")
                    out.leads_created += 1
                    self._record_flag_words(session, company, lead)
            elif _stays_closed(session, company, leads):
                self._keep_closed(session, company, leads, sig, cand, out)
            else:
                _, lead_created = ensure_lead(session, company, "research")
                if lead_created:
                    out.leads_created += 1
        out.sec_kept_as_evidence = len(kept_as_evidence)
        if kept_as_evidence:
            self.log.info("%s SEC filer(s) kept as market evidence, with no lead", len(kept_as_evidence))
        if out.import_notices:  # so far only the rows _keep_closed told of: a person's own rows do not vanish, and one run raises one alert
            session.add(Alert(level="warning", source=self.name, message=(f"{len(out.import_notices)} imported row(s) made no lead: "
                                                                          + "; ".join(out.import_notices))[:500]))

        # Imported files leave data/imports only after their companies are committed.
        for src in file_sources:
            src.finish_intake(session, rows_left_out)
            out.import_files_rejected.extend(src.batch.rejected)
            out.import_notices.extend(src.notices)
            for problem in src.batch.rejected:
                session.add(Alert(level="warning", source=self.name, message=f"Import file not imported: {problem}"[:500]))
            for notice in src.notices:
                session.add(Alert(level="warning", source=self.name, message=f"Import file partly imported: {notice}"[:500]))
        unread = [r for r in out.sources if r.configured and not r.ok]
        if unread:
            alert_once(session, "warning", self.name, "Research source(s) not read this run: "
                       + "; ".join(f"{r.name} ({r.message})" for r in unread)[:400], self.settings, key="research:sources")
        elif inp.sources is None:
            resolve_alerts(session, ("research:sources",), set())
        if stale_alerts:
            alert_once(session, "warning", self.name, "Research source(s) not answering, so no new posts are coming in: "
                       + "; ".join(stale_alerts)[:400], self.settings, key="research:stale")
        elif inp.sources is None:
            resolve_alerts(session, ("research:stale",), set())
        session.commit()
        for src in file_sources:
            out.import_files_archived += src.archive_intake()
        # Every public source failed and your own lists brought nothing: a failure, not "nothing new today". Raised
        # after the commit so that alerts about import files are kept; a retry finds those files gone.
        if public_sources and public_failures == public_sources and not (out.companies_created or out.companies_matched_existing):
            raise TransientAgentError("All configured sources failed: "
                                      + "; ".join(f"{r.name}: {r.message}" for r in unread)[:300])
        return out

    def _record_flag_words(self, session: Session, company: Company, lead: Lead) -> None:
        """What decided a flag set at discovery, where a person reads it. Only words the daily review reads again
        (crm._review_competitor_flags) are written with its prefix: without them it would lift the flag of a true competitor."""
        posts = session.execute(select(Signal.text).where(Signal.company_id == company.id, Signal.signal_type == "hiring")).scalars()
        phrase = next(filter(None, map(competitor_in_post, [*posts, company.description])), None)
        if phrase:
            log_activity(session, f'Marked as a possible competitor: it says "{phrase}"', self.name, type_="research",
                         company_id=company.id, lead_id=lead.id)

    def _keep_closed(self, session: Session, company: Company, leads: list[Lead], sig: Signal | None, cand, out: ResearchOutput) -> None:
        """The post is kept as evidence and no lead is made. hn_hiring re-reads the same threads every morning, so only a post not
        seen before (`sig`) is news. A person is told what they can really do: after an opt-out that is nothing, unless the owner lifts it."""
        said_no = (company_opted_out(session, company) or is_suppressed(session, domain=company.domain)
                   or any(ON_THE_LIST_REASON in (x.disqualify_reason or "") or x.outreach_status == "unsubscribed" for x in leads))
        if cand.source == "csv_import":  # the person's own row: not left_out, because its evidence was kept and finish_intake may reject the file
            out.import_notices.append(f"{company.name}: " + ("it stays closed, after an opt-out or a do-not-contact entry" if said_no
                                                             else "already closed, reopen it on its lead page"))
        if sig is None:
            return
        won = [x for x in leads if x.status == "converted"]
        by_person = [x for x in leads if x.status != "converted" and _closed_by_person(session, x)]
        lead = (won or by_person or leads or [None])[-1]
        log_activity(session, f"New listing from {cand.source} while this company is closed: nothing was reopened", self.name, type_="research",
                     body=cand.source_url, company_id=company.id, lead_id=lead.id if lead else None)
        if lead is None or not (said_no or won or by_person):  # AIME's own close, such as outside the US: on the record only
            return
        told = f"A new listing appeared for closed lead #{lead.id} ({company.name}). Nothing was reopened: " + (
            "it stays closed, because it was closed by an opt-out or the do-not-contact list." if said_no else "a person decides.")
        alert_once(session, "info", self.name, told, self.settings, key=f"listing_on_closed:{lead.id}")
        if won:
            deal = session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.stage == "won")
                                   .order_by(Opportunity.id.desc())).scalars().first()
            _task(session, lead.id, company.id, f"New listing from customer {company.name}",
                  f"{told}\nDeal owner: {(deal.owner if deal else None) or 'none: assign one'}.\nListing: {cand.source_url or 'n/a'}\nNothing has been sent.")
