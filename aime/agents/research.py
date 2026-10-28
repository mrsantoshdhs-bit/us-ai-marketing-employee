"""US Business Research Agent: discovers US companies from authorized public sources and records evidence."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, TransientAgentError
from aime.agents.contracts import SourceReport
from aime.crm_ops import add_signal, ensure_lead, log_activity, upsert_company
from aime.dedupe import find_duplicate
from aime.models import Alert
from aime.sources.base import ResearchFocus
from aime.sources.registry import get_sources
from aime.validation import validate_candidate


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
        transport_failures, configured_count = 0, 0
        file_sources = []  # sources whose files are archived only after this run commits

        country = str(self.settings.section("targeting").get("country") or "US").upper()
        for src in get_sources(self.ctx, inp.sources):
            covers = getattr(src, "countries", ("US",))
            if country not in covers and "*" not in covers:  # discovery follows the configured target country
                out.sources.append(SourceReport(name=src.name, configured=False, ok=False,
                                                message=f"no {country} coverage: this source finds {', '.join(covers)} companies"))
                continue
            ok, msg = src.configured()
            if not ok:
                out.sources.append(SourceReport(name=src.name, configured=False, ok=False, message=msg))
                continue
            configured_count += 1
            try:
                found = src.fetch(focus)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                transport_failures += 1
                out.sources.append(SourceReport(name=src.name, configured=True, ok=False, message=f"network: {exc}"))
                continue
            except httpx.HTTPStatusError as exc:
                out.sources.append(SourceReport(name=src.name, configured=True, ok=False,
                                                message=f"HTTP {exc.response.status_code}"))
                continue
            out.sources.append(SourceReport(name=src.name, configured=True, ok=True, records=len(found), message=msg))
            candidates.extend(found)
            if hasattr(src, "finish_intake"):
                file_sources.append(src)

        if configured_count and transport_failures == configured_count:
            raise TransientAgentError("All configured sources failed with network errors")

        # Focus states/industries first so the per-run limit is spent where the plan points.
        focus_states, focus_inds = set(inp.states), set(inp.industries)
        candidates.sort(key=lambda c: (c.state not in focus_states, c.industry not in focus_inds))

        public_created = 0  # the per-run cap applies to public-source discovery, not to your own CSV lists
        for cand in candidates:
            out.candidates_seen += 1
            if cand.is_us is False:
                out.non_us_skipped += 1
                continue
            if cand.is_us is None:
                out.unknown_location_skipped += 1
                continue
            problems, fixes = validate_candidate(cand)
            if problems:
                out.invalid_skipped += 1
                for p in problems:
                    out.invalid_reasons[p] = out.invalid_reasons.get(p, 0) + 1
                continue
            out.fixes_applied += len(fixes)
            user_list = cand.source == "csv_import"
            if public_created >= limit and not user_list:
                out.limit_reached = True
                # Still attach evidence to companies we already track.
                existing = find_duplicate(session, cand.name, cand.domain, cand.state, cand.source_record_id,
                                          cand.source)
                if existing and add_signal(session, existing, cand.signal_type, cand.signal_text, cand.source,
                                           cand.source_url, cand.observed_at):
                    out.signals_added += 1
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
            if not company.is_competitor:
                _, lead_created = ensure_lead(session, company, "research")
                if lead_created:
                    out.leads_created += 1

        # Imported files leave data/imports only after their companies are committed.
        for src in file_sources:
            src.finish_intake(session)
            out.import_files_rejected.extend(src.batch.rejected)
            for problem in src.batch.rejected:
                session.add(Alert(level="warning", source=self.name, message=f"Import file not imported: {problem}"))
        session.commit()
        for src in file_sources:
            out.import_files_archived += src.archive_intake()
        return out
