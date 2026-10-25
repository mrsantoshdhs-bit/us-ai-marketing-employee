"""User-provided company lists (e.g. an export from your own CRM or an opted-in event list).

Drop CSV files in data/imports/. Columns (header names are case-insensitive):
name (required), website, state, city, industry, notes, source_url
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from aime.dedupe import domain_from_url
from aime.intake import IntakeBatch, file_sha256, mark, seen
from aime.knowledge.geo import normalize_state
from aime.knowledge.industries import INDUSTRIES
from aime.matching import classify_industry
from aime.sources.base import CompanyCandidate, ResearchFocus, Source

# User-provided lists are imported in full (they are not limited by the research per-run cap),
# but a single file is capped so one mistaken export can't flood the CRM.
MAX_ROWS_PER_FILE = 5000


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
        """Read waiting CSV files. Files are archived by `finish_intake` only after the research run commits."""
        out: list[CompanyCandidate] = []
        self.batch = IntakeBatch(folder=self.settings.imports_dir)
        self.loaded: list[tuple[Path, str, int]] = []
        for path in sorted(self.settings.imports_dir.glob("*.csv")):
            sha = file_sha256(path)
            with self.ctx.db.session() as s:
                done = seen(s, "csv", sha)
            if done:  # imported in an earlier run whose archive step failed: only archive it
                self.batch.files.append(path)
                continue
            try:
                rows = read_company_csv(path)
            except (UnicodeDecodeError, csv.Error) as exc:
                self.batch.reject(path, f"unreadable file ({type(exc).__name__})")
                continue
            if len(rows) > MAX_ROWS_PER_FILE:
                self.batch.rejected.append(f"{path.name}: {len(rows)} rows; split it into files of at most "
                                           f"{MAX_ROWS_PER_FILE} rows (file left in place)")
                continue
            self.batch.files.append(path)
            self.loaded.append((path, sha, len(rows)))
            out.extend(rows)
        return out

    def finish_intake(self, session: Session) -> None:
        """Record imported files in the same transaction as their companies (call before commit)."""
        for path, sha, rows in getattr(self, "loaded", []):
            if not seen(session, "csv", sha):
                mark(session, "csv", sha, path.name, rows)

    def archive_intake(self) -> int:
        """Move imported files to processed/ (call after commit)."""
        batch = getattr(self, "batch", None)
        return batch.archive() if batch else 0
