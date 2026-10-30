"""User-provided company lists (e.g. an export from your own CRM or an opted-in event list).

Drop CSV files in data/imports/. Columns (header names are case-insensitive):
name (required), website, state, city, industry, notes, source_url

A bad row does not lose the good rows: it is left out and an alert says which and why. A file none of whose rows
can be used (no name column, or no row that can be imported) goes to rejected/, with an alert.
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from aime.dedupe import domain_from_url
from aime.intake import IntakeBatch, file_sha256, mark, move_aside, seen
from aime.knowledge.geo import normalize_state
from aime.knowledge.industries import INDUSTRIES
from aime.matching import classify_industry
from aime.sources.base import CompanyCandidate, ResearchFocus, Source

log = logging.getLogger("aime.sources.csv_import")

# User-provided lists are imported in full (they are not limited by the research per-run cap),
# but a single file is capped so one mistaken export can't flood the CRM.
MAX_ROWS_PER_FILE = 5000
NAME_COLUMNS = ("name", "company", "company name")
SHOWN_PROBLEMS = 3  # how many left-out rows an alert spells out


class MissingNameColumn(ValueError):
    pass


def read_company_csv(path: Path, problems: list[str] | None = None) -> list[CompanyCandidate]:
    """The usable rows of a file. Each row left out is described in `problems`."""
    problems = [] if problems is None else problems
    out: list[CompanyCandidate] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not {(h or "").strip().lower() for h in reader.fieldnames or []}.intersection(NAME_COLUMNS):
            raise MissingNameColumn(f"no company name column (a header called {', '.join(NAME_COLUMNS)})")
        for i, raw in enumerate(reader, start=2):
            if None in raw:  # more fields than headers, usually an unquoted comma ("Acme, Inc"): no column can be trusted
                problems.append(f"row {i} has more columns than the header (an unquoted comma?)")
                continue
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):  # a spreadsheet's empty line
                continue
            name = next((row[c] for c in NAME_COLUMNS if row.get(c)), None)
            if not name:
                problems.append(f"row {i} has no company name")
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
                extra={"import_file": path.name},
            ))
    return out


class CSVImportSource(Source):
    name = "csv_import"
    countries = ("*",)  # your own imported list: whatever country it holds
    title = "CSV import (your authorized lists)"
    description = "Company lists you provide in data/imports/*.csv"

    def configured(self) -> tuple[bool, str]:
        files = list(self.settings.imports_dir.glob("*.csv"))
        return True, f"{len(files)} file(s) waiting in data/imports"

    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]:
        """Read waiting CSV files. Files are archived by `finish_intake` only after the research run commits."""
        out: list[CompanyCandidate] = []
        self.batch = IntakeBatch(folder=self.settings.imports_dir)
        self.loaded: list[tuple[Path, str, int, list[str]]] = []
        self.unusable: list[Path] = []  # moved to rejected/ with the archive step, once their alert is committed
        self.notices: list[str] = []  # files imported, but with rows left out
        for path in sorted(self.settings.imports_dir.glob("*.csv")):
            sha = file_sha256(path)
            with self.ctx.db.session() as s:
                done = seen(s, "csv", sha)
            if done:  # imported in an earlier run whose archive step failed: only archive it
                self.batch.files.append(path)
                continue
            problems: list[str] = []
            try:
                rows = read_company_csv(path, problems)
            except MissingNameColumn as exc:
                self._reject(path, str(exc))
                continue
            except (UnicodeDecodeError, csv.Error) as exc:
                self._reject(path, f"unreadable file ({type(exc).__name__})")
                continue
            if len(rows) > MAX_ROWS_PER_FILE:
                self.batch.rejected.append(f"{path.name}: {len(rows)} rows; split it into files of at most "
                                           f"{MAX_ROWS_PER_FILE} rows (file left in place)")
                continue
            self.batch.files.append(path)
            self.loaded.append((path, sha, len(rows), problems))
            out.extend(rows)
        return out

    def _reject(self, path: Path, reason: str) -> None:
        self.unusable.append(path)
        self.batch.rejected.append(f"{path.name}: {reason}")

    def finish_intake(self, session: Session, skipped: dict[str, Counter] | None = None) -> None:
        """Record imported files in the same transaction as their companies (call before commit). `skipped` holds,
        per file name, the rows the research agent then left out and why."""
        for path, sha, rows, problems in getattr(self, "loaded", []):
            left_out = (skipped or {}).get(path.name) or Counter()
            total, kept = rows + len(problems), rows - sum(left_out.values())
            reasons = problems + [f"{n} row(s) {why}" for why, n in left_out.items()]
            detail = "; ".join(reasons[:SHOWN_PROBLEMS]) + (f"; and {len(reasons) - SHOWN_PROBLEMS} more"
                                                             if len(reasons) > SHOWN_PROBLEMS else "")
            if total and kept <= 0:  # not "imported": the person can correct the file and drop it in again
                self.batch.files.remove(path)
                self._reject(path, f"none of its {total} row(s) could be imported: {detail}")
                continue
            if not seen(session, "csv", sha):
                mark(session, "csv", sha, path.name, rows)
            if reasons:
                self.notices.append(f"{path.name}: {total - kept} of {total} row(s) left out: {detail}")

    def archive_intake(self) -> int:
        """Move imported files to processed/ and unusable ones to rejected/ (call after commit)."""
        batch = getattr(self, "batch", None)
        if batch is None:
            return 0
        for path in getattr(self, "unusable", []):
            try:
                move_aside(path, batch.folder / "rejected")
            except OSError as exc:  # it stays in the folder: the next run reads it, and says so, again
                log.warning("Could not move %s to rejected/ yet: %s", path.name, exc)
        return batch.archive()
