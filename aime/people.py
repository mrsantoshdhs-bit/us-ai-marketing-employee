"""Decision-maker identification from what a company publishes about itself. Nothing is guessed.

A person is recorded only when a page pairs a real-looking name with a job title:
- schema.org Person data (JSON-LD) with a name and jobTitle, or an Organization's founder (high confidence);
- a name block followed by a title block (or title then name: the page's dominant order is used), as on
  team cards (high confidence on team/about pages, medium elsewhere);
- a single line "Name, Title" / "Name | Title" / "Name - Title";
- a sentence such as "founded by Jane Doe" or "our CEO, Jane Doe" (medium confidence).
The evidence is the exact published text and the page URL. Titles are kept as published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aime.dedupe import normalize_name

TITLE_RE = re.compile(
    r"\b(ceo|cfo|coo|cto|cmo|cio|cro|cpo|chief|founder|co-?founder|owner|president|vice president|vp|svp|evp|"
    r"head of|director|partner|principal|managing|manager|lead|officer|chair(?:man|woman|person)?|executive|"
    r"general counsel|broker|attorney|realtor)\b", re.I)
ROLE_RULES: list[tuple[str, re.Pattern]] = [
    ("executive", re.compile(r"\b(chief executive|ceo|founder|co-?founder|owner|president|managing (director|partner)|"
                             r"principal|general manager|chair(man|woman|person)?|managing broker)\b", re.I)),
    ("operations", re.compile(r"\b(coo|chief operating|operations|ops|customer (experience|success|support))\b", re.I)),
    ("technology", re.compile(r"\b(cto|cio|chief (technology|information|digital|product)|engineering|technology|"
                              r"technical|it\b|information technology|product|data)\b", re.I)),
    ("sales", re.compile(r"\b(cro|chief (revenue|commercial|sales)|sales|revenue|business development|growth|"
                         r"partnerships|accounts?)\b", re.I)),
    ("marketing", re.compile(r"\b(cmo|chief marketing|marketing|brand|demand generation)\b", re.I)),
    ("finance", re.compile(r"\b(cfo|chief financial|finance|financial|controller|accounting)\b", re.I)),
    ("people", re.compile(r"\b(chro|human resources|hr\b|people|talent|recruit)", re.I)),
    # Board members only: "Director of Customer Experience" is a department director, not the board.
    ("board", re.compile(r"^(independent |non-executive )?director$|\b(board member|board of directors|trustee)\b", re.I)),
]
# A title naming another organisation ("Founder, Biddify", "CFO at Acme Corp") belongs to someone from elsewhere:
# a customer quoted in a testimonial, an advisor, a guest. Department words after the separator are fine.
ORG_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|corp|corporation|company|co|labs?|group|partners|bank|university|college|"
                           r"city|county|state|agency|studio|technologies|systems|health|capital|ventures|foundation|"
                           r"institute|association|department|school|hospital|realty|properties|holdings|brands)\b\.?", re.I)
DEPARTMENT_RE = re.compile(
    r"\b(sales|marketing|product|products|engineering|operations|finance|financial|technology|technical|customer|"
    r"customers|people|growth|business|development|strategy|design|data|research|revenue|partnerships|success|support|"
    r"it|hr|human|legal|compliance|communications|brand|content|analytics|innovation|digital|global|enterprise|"
    r"commercial|accounts?|clients?|services|delivery|platform|infrastructure|security|quality|supply|logistics|"
    r"procurement|talent|recruiting|culture|community|education|training|programs?|projects?|portfolio|investor|"
    r"corporate|international|regional|north|south|east|west|central|americas|emea|apac|experience|ai|software|"
    r"solutions|automation|transformation|insights|relations|affairs|administration|planning|medical|clinical|"
    r"staff|board)\b", re.I)
OTHER_ORG_SPLIT = re.compile(r",\s+|\s+(?:at|@)\s+|\s+of\s+(?=[A-Z])")
# Who usually buys each of our services: role categories in order of fit.
SERVICE_ROLES = {
    "custom_crm": ("sales", "operations", "executive", "technology", "marketing"),
    "ai_agents": ("technology", "operations", "executive", "sales"),
    "agent_orchestration": ("technology", "operations", "executive"),
    "bpa": ("operations", "executive", "technology", "finance"),
    "leadgen_automation": ("sales", "marketing", "executive"),
    "sales_automation": ("sales", "executive", "operations", "marketing"),
    "saas_dev": ("technology", "executive", "operations"),
    "custom_software": ("technology", "executive", "operations"),
    "ai_consulting": ("executive", "technology", "operations"),
}
NAME_STOPWORDS = {
    "our", "the", "team", "meet", "about", "us", "contact", "leadership", "management", "board", "directors",
    "read", "more", "view", "profile", "company", "services", "service", "solutions", "home", "learn", "get",
    "started", "privacy", "policy", "terms", "careers", "join", "news", "blog", "login", "sign", "book", "demo",
    "call", "schedule", "free", "quote", "client", "clients", "partners", "office", "offices", "inc", "llc", "ltd",
    "corp", "group", "agency", "studio", "labs", "digital", "media", "support", "success", "customer", "customers",
    "case", "studies", "testimonials", "industries", "resources", "events", "press", "linkedin", "twitter",
    "facebook", "instagram", "email", "phone", "address", "street", "suite", "avenue", "road", "united", "states",
    "america", "copyright", "rights", "reserved", "menu", "close", "open", "back", "next", "previous", "all",
    "and", "of", "for", "with", "to", "in", "on", "at", "by", "we", "you", "your", "is", "are", "how", "what",
    "why", "who", "where", "when", "new", "real", "estate", "software", "technology", "data", "cloud", "ai",
    "products", "product", "platform", "pricing", "features", "overview", "mission", "vision", "values", "story",
    "history", "culture", "community", "work", "works", "portfolio", "projects", "insights", "faq",
}
NAME_PART = r"[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*|Mc[A-Z][a-z]+|Mac[A-Z][a-z]+"
NAME_PATTERN = rf"(?:{NAME_PART})(?:\s+[A-Z]\.)?(?:\s+(?:{NAME_PART})){{1,2}}"
SPLIT_RE = re.compile(r"^(?P<name>[^,|–—]{3,60}?)\s*(?:,|\||–|—|\s-\s)\s*(?P<title>[^|]{2,90})$")
SENTENCE_RES = [
    (re.compile(rf"\b(?P<verb>co-?founded|founded|started) (?:in \d{{4}} )?by (?P<name>{NAME_PATTERN})"), None),
    (re.compile(rf"\b(?:our|the) (?P<title>co-?founder(?: and ceo)?|founder(?: and ceo)?|ceo|president|owner|"
                rf"managing partner|chief executive officer),? (?P<name>{NAME_PATTERN})", re.I), None),
    (re.compile(rf"(?P<name>{NAME_PATTERN}),? (?:is )?(?:our |the )?(?P<title>co-?founder(?: and ceo)?|founder(?: and ceo)?|"
                rf"ceo|president|owner|managing partner|chief executive officer)\b"), None),
]


@dataclass
class PersonFinding:
    full_name: str
    title: str
    role_category: str
    confidence: str  # high | medium
    evidence: str
    evidence_url: str
    source: str = "website"  # website | sec_filing

    @property
    def key(self) -> str:
        return normalize_name(self.full_name)


def clean_title(text: str) -> str:
    return " ".join(text.strip(" ,|·•–—-:").split())[:120]


def names_other_org(title: str, company: str | None = None) -> bool:
    """True when the title says the person works somewhere else ("Founder, Biddify", "CFO at Acme Corp")."""
    parts = OTHER_ORG_SPLIT.split(title, maxsplit=1)
    rest = parts[1].strip(" .") if len(parts) == 2 else ""
    if not rest:
        return False
    if company:
        own, other = normalize_name(company), normalize_name(rest)
        if own and other and (other in own or own in other or set(other.split()) & {w for w in own.split() if len(w) > 3}):
            return False  # the title names this company itself
    if ORG_SUFFIX_RE.search(rest):
        return True
    if DEPARTMENT_RE.search(rest):
        return False
    return rest[:1].isupper()  # a proper name that is not a department: another organisation


def is_title(text: str) -> bool:
    t = clean_title(text)
    if not 2 <= len(t) <= 90 or len(t.split()) > 12:
        return False
    if re.search(r"[.!?]\s+[A-Z]", t) or "@" in t or "http" in t.lower():
        return False  # a sentence, an email or a link, not a job title
    return bool(TITLE_RE.search(t))


def looks_like_name(text: str, company: str | None = None) -> bool:
    t = text.strip(" ,|·•–—-:")
    if not 4 <= len(t) <= 60 or TITLE_RE.search(t):
        return False
    tokens = t.split()
    if not 2 <= len(tokens) <= 4:
        return False
    for i, tok in enumerate(tokens):
        core = tok.strip(".,")
        if core.lower() in NAME_STOPWORDS:
            return False
        if len(core) == 1:
            if not (core.isupper() and 0 < i < len(tokens) - 1):
                return False  # an initial only in the middle ("Jane A. Doe")
            continue
        letters = core.replace("-", "").replace("'", "").replace("’", "")
        if not (letters.isalpha() and core[0].isupper()) or (core.isupper() and len(core) > 2):
            return False
        if not any(ch.islower() for ch in core):
            return False
    if company:
        norm, comp = normalize_name(t), normalize_name(company)
        if norm and comp and (norm in comp or comp in norm):
            return False
    return True


def role_category(title: str) -> str:
    for category, rx in ROLE_RULES:
        if rx.search(title):
            return category
    return "other"


def seniority(title: str) -> int:
    """3 founder/owner/C-level, 2 VP/head/partner, 1 director, 0 lead/manager/other, -1 junior. Advisors rank 1."""
    t = title.lower()
    if re.search(r"\b(advis[oe]ry?|advisor|adviser|emeritus)\b", t):
        return 1
    if re.search(r"\b(intern|assistant|associate|coordinator|specialist|trainee|junior)\b", t):
        return -1
    if re.search(r"\b(chief|ceo|coo|cto|cro|cmo|cio|cfo|founder|co-?founder|owner|president)\b", t) and "vice president" not in t:
        return 3
    if re.search(r"\b(vp|vice president|svp|evp|head of|managing|partner|principal)\b", t):
        return 2
    if re.search(r"\bdirector\b", t):
        return 1
    return 0


def relevance(category: str, title: str, service: str | None) -> int:
    """0-100: how likely this person decides on our service. Seniority first (40 + 15 per level), then
    function fit for the service: +20 / +12 / +6 for the first three role categories that usually buy it;
    executives always get +12 because at small companies they decide most purchases."""
    order = SERVICE_ROLES.get(service or "", ("executive", "operations", "sales", "technology", "marketing"))
    fit = {0: 20, 1: 12, 2: 6}.get(order.index(category), 0) if category in order else 0
    if category == "executive":
        fit = max(fit, 12)
    return max(0, min(100, 40 + 15 * seniority(title) + fit))


def _jsonld_people(obj, url: str, out: list[PersonFinding], company: str | None = None) -> None:
    if isinstance(obj, list):
        for item in obj:
            _jsonld_people(item, url, out, company)
        return
    if not isinstance(obj, dict):
        return
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Person" in types and isinstance(obj.get("name"), str) and isinstance(obj.get("jobTitle"), str):
        name, title = " ".join(obj["name"].split()), clean_title(obj["jobTitle"])
        if looks_like_name(name) and is_title(title) and not names_other_org(title, company):
            out.append(PersonFinding(name, title, role_category(title), "high",
                                     f'schema.org Person: "{name}", jobTitle "{title}"', url))
    for key in ("founder", "founders"):
        for f in (obj.get(key) if isinstance(obj.get(key), list) else [obj.get(key)]):
            if isinstance(f, dict) and isinstance(f.get("name"), str) and looks_like_name(f["name"]):
                title = clean_title(f["jobTitle"]) if isinstance(f.get("jobTitle"), str) else "Founder"
                out.append(PersonFinding(" ".join(f["name"].split()), title, role_category(title), "high",
                                         f'schema.org Organization {key}: "{f["name"]}"', url))
    for key in ("@graph", "employee", "employees", "member", "members"):  # not review authors
        if key in obj:
            _jsonld_people(obj[key], url, out, company)


def _pairs(items: list[str], url: str, company: str | None, confidence: str, out: list[PersonFinding]) -> None:
    """Adjacent name/title items. The page's dominant order (name first or title first) decides ties."""
    names = [i for i, t in enumerate(items) if looks_like_name(t, company)]
    if not names:
        return
    nxt = sum(1 for i in names if i + 1 < len(items) and is_title(items[i + 1]) and not looks_like_name(items[i + 1]))
    prv = sum(1 for i in names if i > 0 and is_title(items[i - 1]) and not looks_like_name(items[i - 1]))
    title_first = prv > nxt
    for i in names:
        j = i - 1 if title_first else i + 1
        if (0 <= j < len(items) and is_title(items[j]) and not looks_like_name(items[j], company)
                and not names_other_org(items[j], company)):
            name, title = items[i].strip(" ,|·•–—-:"), clean_title(items[j])
            evidence = f"{title} — {name}" if title_first else f"{name} — {title}"
            out.append(PersonFinding(name, title, role_category(title), confidence, evidence, url))


def extract_people(pages, company: str | None = None) -> list[PersonFinding]:
    """Name/title cards and "Name, Title" lines count only on team and about pages: on homepages and other
    pages they are mostly customer testimonials. Elsewhere only structured data and explicit sentences
    ("our CEO, Jane Doe", "founded by Jane Doe") are used."""
    found: list[PersonFinding] = []
    for page in pages:
        if not page.ok:
            continue
        for obj in page.jsonld:
            _jsonld_people(obj, page.url, found, company)
        on_team_page = page.kind in ("team", "about")
        if on_team_page:
            _pairs([t for _, t in page.blocks], page.url, company, "high", found)
            _pairs(page.segments, page.url, company, "high", found)
        for _, text in page.blocks:
            m = SPLIT_RE.match(text) if on_team_page else None
            if m and looks_like_name(m["name"], company) and is_title(m["title"]) and not names_other_org(m["title"], company):
                title = clean_title(m["title"])
                found.append(PersonFinding(m["name"].strip(), title, role_category(title), "high", text, page.url))
            if len(text) < 30:
                continue
            for rx, _ in SENTENCE_RES:
                for m in rx.finditer(text):
                    name = m["name"]
                    if not looks_like_name(name, company):
                        continue
                    if "verb" in m.groupdict() and m["verb"]:
                        title = "Co-founder" if m["verb"].lower().startswith("co") else "Founder"
                    else:
                        title = clean_title(m["title"]).capitalize() if m["title"].islower() else clean_title(m["title"])
                    sentence = _sentence_around(text, m.start(), m.end())
                    if re.match(r"\s+(?:of|at)\s+[A-Z]", text[m.end():]):
                        continue  # "Jane Doe, founder of Acme": someone from another company
                    found.append(PersonFinding(name, title, role_category(title), "medium", sentence, page.url))
    return merge_findings(found)


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start) + 2, 0)
    right = text.find(". ", end)
    return text[left: right + 1 if right != -1 else len(text)].strip()[:300]


def merge_findings(found: list[PersonFinding]) -> list[PersonFinding]:
    """One finding per person: the highest confidence, then the most specific title."""
    best: dict[str, PersonFinding] = {}
    rank = {"high": 2, "medium": 1}
    for f in found:
        cur = best.get(f.key)
        if cur is None or (rank[f.confidence], len(f.title)) > (rank[cur.confidence], len(cur.title)):
            best[f.key] = f
    return list(best.values())


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    return " ".join(parts[:-1]), parts[-1]
