"""The country framework: which countries the AI employee works in, and the rules that come with each one.

Before Option B the US was written into four places (the sources' coverage, the research filter, the company record and
the scoring rule). They now ask this module instead:

- `target_countries`: the countries whose companies are accepted (research keeps them, scoring does not disqualify them,
  and a new company is recorded with its country). These are the enabled countries of the registry (settings section
  "countries"); a country other than the US counts only once a legal review of its email-marketing rules is recorded for it
  (CASL, GDPR/PECR, DPDP and the like differ from CAN-SPAM, and this code does not pre-judge them). The rule is enforced when
  a country is saved, and again here, so a hand-edited database cannot get round it.
- `discovery_countries`: the countries discovery asks the sources for. With no saved registry this is the Target country of
  the sales settings, exactly as before.

The default registry is today's setup: the US enabled, with the send window, time zone and holidays of the follow-up
settings, under CAN-SPAM. A Target country other than the US set in the sales settings (as before Option B) is listed as
well, disabled and without a legal review: discovery still asks the sources for it, as before, and nothing from it is
accepted until it is reviewed and enabled.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

US_REASON = "Outside US target market"  # the scoring text before Option B; kept word for word for a US-only setup
COUNTRY_NAMES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "IE": "Ireland", "AU": "Australia", "NZ": "New Zealand",
    "IN": "India", "SG": "Singapore", "DE": "Germany", "FR": "France", "NL": "Netherlands", "ES": "Spain", "IT": "Italy",
    "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland", "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "PL": "Poland", "PT": "Portugal", "MX": "Mexico", "BR": "Brazil", "AE": "United Arab Emirates", "ZA": "South Africa",
    "JP": "Japan", "IL": "Israel",
}
_ALIASES = {"usa": "US", "u.s.": "US", "u.s.a.": "US", "united states of america": "US", "america": "US", "uk": "GB",
            "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "great britain": "GB", "britain": "GB", "uae": "AE"}
_BY_NAME = {name.lower(): code for code, name in COUNTRY_NAMES.items()}


def normalize_country(value: str | None) -> str | None:
    """A two-letter code from a code or a common English name ("Canada", "UK", "USA"); None when it cannot be told."""
    text = re.sub(r"\s+", " ", (value or "").strip()).lower()
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    if text in _BY_NAME:
        return _BY_NAME[text]
    if re.fullmatch(r"[a-z]{2}", text) and text.upper() in COUNTRY_NAMES:
        return text.upper()
    return None


def primary_country(settings) -> str:
    return str(settings.section("targeting").get("country") or "US").strip().upper()


def default_registry(settings) -> list[dict]:
    f = settings.section("followup")
    us = {"code": "US", "enabled": True, "name": "United States",
          "timezone": f.get("send_window_timezone") or "America/New_York",
          "send_window_start": f.get("send_window_start") or "08:00", "send_window_end": f.get("send_window_end") or "18:00",
          "holidays": list(f.get("holidays") or []), "compliance_regime": "CAN-SPAM (US)", "language": "en",
          "legal_review_confirmed_by": None, "legal_review_confirmed_at": None, "legal_review_note": None}
    out = [us]
    primary = primary_country(settings)
    if primary != "US" and re.fullmatch(r"[A-Z]{2}", primary):
        out.append({**us, "code": primary, "enabled": False, "name": COUNTRY_NAMES.get(primary, primary), "holidays": [],
                    "compliance_regime": "not reviewed"})
    return out


def registry(settings) -> list[dict]:
    from aime import settings_store

    return settings_store.get(settings, "countries")["countries"]


def country(settings, code: str) -> dict | None:
    return next((c for c in registry(settings) if c["code"] == code), None)


def cleared(entry: dict) -> bool:
    """May this country's companies be worked: the US, or another country with a recorded legal review."""
    return entry["code"] == "US" or bool(entry.get("legal_review_confirmed_by") and entry.get("legal_review_confirmed_at"))


def configured_countries(settings) -> list[str]:
    """Enabled in the registry, reviewed or not."""
    return [c["code"] for c in registry(settings) if c.get("enabled")]


def target_countries(settings) -> list[str]:
    """The enabled countries that are cleared: their companies are kept, recorded and scored."""
    return [c["code"] for c in registry(settings) if c.get("enabled") and cleared(c)]


def discovery_countries(settings) -> list[str]:
    from aime import settings_store

    primary = primary_country(settings)
    if not settings_store.stored(settings, "countries"):
        return [primary]
    targets = target_countries(settings)
    return ([primary] if primary in targets else []) + [c for c in targets if c != primary]


def outside_reason(targets) -> str:
    targets = list(targets)
    if targets == ["US"]:
        return US_REASON
    return f"Outside the enabled target countries ({', '.join(targets) or 'none'})"


def us_only(settings) -> bool:
    return target_countries(settings) == ["US"]


def candidate_country(cand) -> str | None:
    """Where a discovered company is, from its own evidence: the source's country, else its US evidence. "outside" for
    a place known not to be the US but not named; None when nothing places it."""
    code = normalize_country(getattr(cand, "country", None))
    if code:
        return code
    if cand.is_us is True:
        return "US"
    if cand.is_us is False:
        return "outside"
    return None


def sources_for(settings, code: str) -> list[str]:
    """The enabled discovery sources that cover a country (a source for "*", such as your CSV lists, covers any)."""
    from aime.sources.registry import source_settings

    return [s["name"] for s in source_settings(settings) if s["enabled"] and (code in s["countries"] or "*" in s["countries"])]


def record_legal_review(session: Session, settings, code: str, confirmed_by: str, actor: str, note: str = "") -> list[str]:
    """Record that the owner (or their adviser) has reviewed this country's email-marketing rules: the only way the review
    fields change, audited. The country is added to the registry if missing, still disabled; enabling it is a separate save."""
    from aime import settings_store
    from aime.automation import audit

    code = (code or "").strip().upper()
    who = (confirmed_by or "").strip()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError("give a two-letter country code")
    if code == "US":
        raise ValueError("the US needs no recorded review here: the system was built for CAN-SPAM")
    if len(who) < 3:
        raise ValueError("give the name of the person who confirmed the legal review")
    data = settings_store.get(settings, "countries")
    entry = next((c for c in data["countries"] if c["code"] == code), None)
    if entry is None:
        us = next((c for c in data["countries"] if c["code"] == "US"), data["countries"][0])
        entry = {**us, "code": code, "enabled": False, "name": COUNTRY_NAMES.get(code, code), "holidays": [],
                 "compliance_regime": "to be set"}
        data["countries"].append(entry)
    entry["legal_review_confirmed_by"] = who[:80]
    entry["legal_review_confirmed_at"] = settings_store.utc_now().isoformat(timespec="seconds")
    entry["legal_review_note"] = (note or "").strip()[:300] or None
    _, changes = settings_store.save_trusted(session, settings, "countries", data, actor,
                                             f"legal review of {code} recorded", "country_legal_review_recorded")
    audit(session, actor, "country_legal_review", f"country:{code}", f"confirmed by {who[:80]}" + (f": {note[:200]}" if note else ""))
    return changes
