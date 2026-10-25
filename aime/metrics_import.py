"""Import LinkedIn post analytics exported by the user (CSV) into content_metrics.

Member-post analytics are not generally available through LinkedIn's self-serve API, so the
supported free path is: export analytics from LinkedIn, drop the CSV in data/imports/linkedin_metrics/.
Recognized columns (case-insensitive): post url / url / post link, impressions, reactions / likes,
comments, reposts / shares, clicks.
"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aime.intake import IntakeBatch, file_sha256, mark, seen
from aime.models import ContentItem, ContentMetric

ALIASES = {
    "url": ("post url", "url", "post link", "link", "post_url"),
    "impressions": ("impressions", "views"),
    "reactions": ("reactions", "likes"),
    "comments": ("comments",),
    "shares": ("reposts", "shares"),
    "clicks": ("clicks",),
}


def _num(v: str | None) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def import_metrics_dir(session: Session, folder: Path) -> tuple[int, IntakeBatch]:
    """Import metrics CSVs once each. The caller commits, then calls `batch.archive()` (see aime.intake)."""
    folder.mkdir(parents=True, exist_ok=True)
    count = 0
    batch = IntakeBatch(folder=folder)
    for path in sorted(folder.glob("*.csv")):
        sha = file_sha256(path)
        if seen(session, "metrics", sha):
            batch.files.append(path)  # already imported; only the archive step is left
            continue
        rows = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = [{(k or "").strip().lower(): v for k, v in raw.items()} for raw in csv.DictReader(fh)]
        except (UnicodeDecodeError, csv.Error) as exc:
            batch.reject(path, f"unreadable file ({type(exc).__name__})")
            continue
        for row in rows:
            pick = {key: next((row[a] for a in names if a in row), None) for key, names in ALIASES.items()}
            ref = (pick["url"] or "").strip()
            if not ref:
                continue
            item = session.execute(select(ContentItem).where(or_(
                ContentItem.external_url == ref, ContentItem.external_id == ref,
                ContentItem.external_url.like(f"%{ref.rstrip('/').rsplit('/', 1)[-1]}%")))).scalars().first()
            session.add(ContentMetric(
                content_id=item.id if item else None, external_ref=ref[:400],
                impressions=_num(pick["impressions"]), reactions=_num(pick["reactions"]),
                comments=_num(pick["comments"]), shares=_num(pick["shares"]), clicks=_num(pick["clicks"]),
                source="csv_import"))
            count += 1
        mark(session, "metrics", sha, path.name, len(rows))
        batch.files.append(path)
    return count, batch
