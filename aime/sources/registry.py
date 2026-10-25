"""Registry of company-discovery sources."""

from __future__ import annotations

from aime.sources.base import Source
from aime.sources.csv_import import CSVImportSource
from aime.sources.hn_hiring import HNHiringSource
from aime.sources.sec_edgar import SECEdgarSource

SOURCE_CLASSES: dict[str, type[Source]] = {
    HNHiringSource.name: HNHiringSource,
    SECEdgarSource.name: SECEdgarSource,
    CSVImportSource.name: CSVImportSource,
}


def get_sources(ctx, names: list[str] | None = None) -> list[Source]:
    selected = names or list(SOURCE_CLASSES)
    return [SOURCE_CLASSES[n](ctx) for n in selected if n in SOURCE_CLASSES]
