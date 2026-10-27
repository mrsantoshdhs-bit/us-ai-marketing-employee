"""Officers of SEC-registered companies from their insider filings (Forms 3 and 4): official and free.

Each filing names the reporting person and, for officers, their title as filed. Names are filed as
"LAST FIRST MIDDLE"; they are shown as "First Middle Last" and the filed form is kept in the evidence.
Directors without an officer title are recorded as "Director". A title filed as "See Remarks" is not
guessed: such a person is kept only if they are also a director.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from aime.people import PersonFinding, role_category

log = logging.getLogger("aime.sources.sec_officers")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index.htm"
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd"}
# Surname particles: "VAN DYKE RICHARD J" is Richard J. Van Dyke, "DE LA CRUZ MARIA" is Maria De La Cruz.
PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "da", "di", "du", "dos", "das", "la", "le", "st",
             "ter", "ten", "bin", "al", "el", "mac", "o"}


def _flag(rel, name: str) -> bool:
    return _text(rel, name).lower() in ("1", "true")


def _text(el, path: str) -> str:
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _cap(token: str) -> str:
    return "-".join("'".join(p[:1].upper() + p[1:].lower() for p in part.split("'")) for part in token.split("-"))


def display_name(filed: str) -> str:
    """"DOE JOHN A" or "Doe, John A." -> "John A. Doe"; suffixes stay at the end."""
    raw = " ".join(filed.replace(".", ". ").split())
    if "," in raw:
        last, given = raw.split(",", 1)
        tokens = given.split() + [last.strip()]
    else:
        parts = raw.split()
        n = 0
        while n < len(parts) - 2 and parts[n].lower().strip(".") in PARTICLES:
            n += 1
        tokens = parts[n + 1:] + parts[:n + 1] if len(parts) > n + 1 else parts
    suffix = [t for t in tokens if t.lower().strip(".") in SUFFIXES]
    tokens = [t for t in tokens if t not in suffix]
    out = []
    for t in tokens:
        core = t.strip(".")
        out.append(core.upper() + "." if len(core) == 1 else _cap(core))
    return " ".join(out + [s.strip(".").capitalize() + "." for s in suffix])


def _same_cik(a: str, b: str) -> bool:
    return a.strip().lstrip("0") == b.strip().lstrip("0")


def parse_ownership_xml(xml_text: str, issuer_cik: str | None = None) -> list[dict]:
    """Reporting persons and their relationship to the issuer. With `issuer_cik`, a filing about another issuer
    (this company reporting its own stake in someone else) gives nobody, and the company itself is never a person."""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 -- official SEC filings; expat resolves no external entities
    except ET.ParseError:
        return []
    if issuer_cik and not _same_cik(_text(root, "issuer/issuerCik") or issuer_cik, issuer_cik):
        return []
    people = []
    for owner in root.iter("reportingOwner"):
        filed = _text(owner, "reportingOwnerId/rptOwnerName")
        rel = owner.find("reportingOwnerRelationship")
        if not filed or rel is None:
            continue
        if issuer_cik and _same_cik(_text(owner, "reportingOwnerId/rptOwnerCik") or "-", issuer_cik):
            continue
        people.append({"filed_name": filed, "is_officer": _flag(rel, "isOfficer"), "is_director": _flag(rel, "isDirector"),
                       "officer_title": " ".join(_text(rel, "officerTitle").split())})
    return people


def _raw_document(primary: str) -> str:
    return re.sub(r"^xslF345X\d+/", "", primary)


def sec_officers(ctx, cik: str, max_filings: int = 12, since_days: int = 730) -> list[PersonFinding]:
    """Officers (and directors) named in the company's recent Form 3/4 filings."""
    ua = ctx.settings.sec_user_agent.strip()
    if not ua or "@" not in ua:
        return []
    cik10 = cik.zfill(10)
    found: dict[str, PersonFinding] = {}
    with ctx.http(headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}) as client:
        resp = client.get(SUBMISSIONS.format(cik=cik10))
        if resp.status_code != 200:
            return []
        recent = (resp.json().get("filings") or {}).get("recent") or {}
        cutoff = date.today() - timedelta(days=since_days)
        rows = zip(recent.get("form", []), recent.get("accessionNumber", []), recent.get("primaryDocument", []),
                   recent.get("filingDate", []), strict=False)
        checked = 0
        for form, accession, primary, filed_on in rows:
            if form not in ("3", "4") or not primary or checked >= max_filings:
                continue
            try:
                if date.fromisoformat(filed_on) < cutoff:
                    continue
            except ValueError:
                continue
            checked += 1
            acc = accession.replace("-", "")
            time.sleep(0.15)  # stay well under SEC's 10 requests/second
            doc = client.get(ARCHIVE.format(cik=int(cik10), acc=acc, doc=_raw_document(primary)))
            if doc.status_code != 200:
                continue
            url = INDEX.format(cik=int(cik10), acc=acc, accession=accession)
            for p in parse_ownership_xml(doc.text, cik10):
                title = p["officer_title"] if p["is_officer"] and not re.search(r"see remarks", p["officer_title"], re.I) else ""
                if not title and p["is_director"]:
                    title = "Director"
                if not title:
                    continue
                name = display_name(p["filed_name"])
                evidence = (f'SEC Form {form} filed {filed_on}: reporting person "{p["filed_name"]}", '
                            f'{"officer title" if title != "Director" else "relationship"} "{title}"')
                finding = PersonFinding(name, title, role_category(title) if title != "Director" else "board",
                                        "high", evidence, url, source="sec_filing")
                found.setdefault(finding.key, finding)
    return list(found.values())
