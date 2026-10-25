"""User-provided company lists (e.g. an export from your own CRM or an opted-in event list).

Drop CSV files in data/imports/. Columns (header names are case-insensitive):
name (required), website, state, city, industry, notes, source_url
"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

from aime.dedupe import domain_from_url
from aime.knowledge.geo import normalize_state
from aime.knowledge.industries import INDUSTRIES
from aime.matching import classify_industry
from aime.sources.base import CompanyCandidate, ResearchFocus, Source


def read_company_csv(path: Path) -> list[CompanyCandidate]:
    out: list[CompanyCandidate] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            name = row.get("name") or row.get("company") or row.get("company name")
            if not name:
                continue
            website = row.get("website") or None
            state = normalize_state(row.get("state"))
            notes = row.get("notes") or ""
            industry = row.get("industry") or None
            if industry not in INDUSTRIES:
                industry = classify_industry(f"{industry or ''} {notes}")
            out.append(CompanyCandidate(
                name=name,
                source="csv_import",
                source_url=row.get("source_url") or f"file://{path.name}#row{i}",
                source_record_id=f"csv:{path.name}:{i}",
                website=website,
                domain=domain_from_url(website),
                state=state,
                city=row.get("city") or None,
                is_us=True if state else None,
                industry=industry,
                description=notes or None,
                signal_text=notes or None,
                signal_type="import",
                observed_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None),
            ))
    return out


class CSVImportSource(Source):
    name = "csv_import"
    title = "CSV import (your authorized lists)"
    description = "Company lists you provide in data/imports/*.csv"

    def configured(self) -> tuple[bool, str]:
        files = list(self.settings.imports_dir.glob("*.csv"))
        return True, f"{len(files)} file(s) waiting in data/imports"

    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]:
        out: list[CompanyCandidate] = []
        processed = self.settings.imports_dir / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.settings.imports_dir.glob("*.csv")):
            out.extend(read_company_csv(path))
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.move(str(path), processed / f"{stamp}_{path.name}")
        return out
