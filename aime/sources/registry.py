"""Registry of company-discovery sources, and their settings (section "sources": on or off, priority, countries).

The .env switches (HN_SOURCE_ENABLED, SEC_USER_AGENT ...) still turn a source off whatever the settings say: a source
switched on here but not configured reports why, as before. Priority 1 is read first, so its companies come first within
the research per-run cap; the defaults keep the order below, which is the order discovery has always used."""

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


def default_source_settings() -> list[dict]:
    return [{"name": name, "enabled": True, "priority": (i + 1) * 10, "countries": list(getattr(cls, "countries", ("US",)))}
            for i, (name, cls) in enumerate(SOURCE_CLASSES.items())]


def source_settings(settings) -> list[dict]:
    """Every registered source with its settings, by priority (ties keep the registry's order). A source missing from a
    saved version (added to the code later) gets its defaults."""
    from aime import settings_store

    saved = {s["name"]: s for s in settings_store.get(settings, "sources")["sources"]}
    rows = [saved.get(d["name"], d) for d in default_source_settings()]
    order = list(SOURCE_CLASSES)
    return sorted(rows, key=lambda r: (r["priority"], order.index(r["name"])))


def ordered_sources(ctx, names: list[str] | None = None) -> list[tuple[Source, dict]]:
    """The sources to read, by priority, each with its settings (enabled, priority, countries). With no saved source
    settings, a run that names its sources reads them in the order given, as it always did."""
    from aime import settings_store

    rows = source_settings(ctx.settings)
    if names:
        if settings_store.stored(ctx.settings, "sources"):
            rows = [r for r in rows if r["name"] in set(names)]
        else:
            by_name = {r["name"]: r for r in rows}
            rows = [by_name[n] for n in names if n in by_name]
    return [(SOURCE_CLASSES[r["name"]](ctx), r) for r in rows]
