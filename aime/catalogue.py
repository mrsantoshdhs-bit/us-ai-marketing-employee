"""The editable catalogue of industries and services (settings section "catalogue").

It is seeded from the code's own lists (aime.knowledge.industries and aime.knowledge.services), whose keys keep working
everywhere: a built-in entry can be relabelled or disabled, never removed. A Super Admin can add entries of their own; such an
entry is offered in the sales settings and can be targeted, while its keywords for classifying posts stay those of the
built-in entries (only the code's entries carry classification rules). A disabled entry is no longer offered and is left out
of the target lists the agents read; nothing already on file is changed.
"""

from __future__ import annotations


def default_catalogue() -> dict:
    from aime.knowledge.industries import INDUSTRIES
    from aime.knowledge.services import SERVICES

    return {
        "industries": [{"key": k, "label": i.label, "enabled": True, "builtin": True, "description": i.audience}
                       for k, i in INDUSTRIES.items()],
        "services": [{"key": k, "label": s.name, "enabled": True, "builtin": True, "description": s.pitch[:300]}
                     for k, s in SERVICES.items()],
    }


def entries(settings, kind: str) -> list[dict]:
    from aime import settings_store

    return settings_store.get(settings, "catalogue")[kind]


def enabled(settings, kind: str) -> dict[str, str]:
    """key -> label of the enabled entries of "industries" or "services", in catalogue order."""
    return {e["key"]: e["label"] for e in entries(settings, kind) if e["enabled"]}


def keep_enabled(settings, kind: str, keys) -> list:
    """`keys` without the ones a Super Admin disabled (unknown keys are kept: the catalogue only ever takes away)."""
    off = {e["key"] for e in entries(settings, kind) if not e["enabled"]}
    return [k for k in keys if k not in off]
