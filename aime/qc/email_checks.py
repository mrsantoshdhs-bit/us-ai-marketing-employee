"""Quality checks for outreach emails, run when a draft is written and again when it is edited.

fail      -> the draft must be fixed (or explicitly overridden) before it can be approved
uncertain -> held: a person must look (for example a number outside the quoted evidence)
pass      -> fine
Quoted evidence is the company's own words: it must appear verbatim in the source, and it is skipped
by the spelling, cliché and claims checks.
"""

from __future__ import annotations

import re

from aime.contact_validation import STATUS_OK
from aime.dedupe import company_to_address
from aime.qc import CheckResult, build_report
from aime.qc.checks import check_cliches, check_grammar, check_spelling
from aime.reply_intel import REPLY_WORDS

MIN_WORDS, MAX_WORDS = 40, 160
SPAM_RE = re.compile(r"\b(free|guarantee[ds]?|risk[- ]free|act now|limited time|urgent|winner|cash|buy now|"
                     r"no obligation|special promotion|click here|100%|once in a lifetime|exclusive deal|"
                     r"amazing|incredible|best in class|#1)\b|\${2,}|!{2,}", re.I)
CLAIM_RE = re.compile(r"\b(we (increased|doubled|tripled|saved|cut|helped)|clients like|customers like|trusted by|"
                      r"we('ve| have)( already| also)? (worked|partnered|done this|built|delivered|served|helped)|"
                      r"we (already )?(worked|partnered|delivered|served|built this|did this)|"
                      r"(clients|customers) (include|such as)|"
                      r"leading|leader in|award[- ]winning|proven to|industry[- ]best|world[- ]class)\b", re.I)
QUOTE_RE = re.compile(r'"([^"]{8,})"')
LINK_RE = re.compile(r"https?://|www\.", re.I)
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# What mail programs put before a subject, in any language, and what a hand can type for it ("Re - x").
_REPLY_PREFIX = re.compile(rf"\s*(?:(?:{REPLY_WORDS})\s*[:\uff1a]|re\s*-\s)\s*", re.I)
PLAIN_RE = re.compile(r"\s*re:\s*", re.I)  # all that an answer of ours ever starts with


def split_footer(body: str) -> tuple[str, str]:
    parts = body.split("\n---\n", 1)
    return parts[0], (parts[1] if len(parts) == 2 else "")


def _without_quotes(text: str) -> str:
    return QUOTE_RE.sub('"quote"', text)  # a neutral placeholder keeps spacing and punctuation intact


def _lowercase_words_only(text: str) -> str:
    """Names (people, companies, products) are capitalised and not dictionary words: spell-check the rest."""
    return re.sub(r"\b[A-Z][\w'’-]*", " ", text)


def _normalize(text: str) -> str:
    return " ".join(text.replace("“", '"').replace("”", '"').split()).lower()


def run_email_checks(subject: str, body: str, evidence: list[dict], contact, suppressed: bool, step: int = 1,
                     sent_subjects: list[str] | None = None) -> dict:
    """`sent_subjects`: the subjects of the emails really sent earlier in this one's thread (aime.outreach.sent_earlier)."""
    main, footer = split_footer(body)
    words = len(re.findall(r"\b\w+\b", main))
    results = [CheckResult("length", "pass" if MIN_WORDS <= words <= MAX_WORDS else "fail",
                           f"{words} words (keep it between {MIN_WORDS} and {MAX_WORDS})")]

    subj_problems = []
    # "Re:" is true only of an answer to an email we really sent in this thread, under the subject the thread began with.
    seen = _INVISIBLE.sub("", subject)
    prefix = _REPLY_PREFIX.match(seen)
    first = PLAIN_RE.sub("", sent_subjects[0]) if sent_subjects else None
    plain = PLAIN_RE.match(seen)
    answers = seen == subject and first is not None and bool(plain) and _normalize(seen[plain.end():]) == _normalize(first)
    if len(seen[plain.end():] if answers else subject) > 70:
        subj_problems.append("longer than 70 characters")
    if prefix and not answers:
        subj_problems.append("pretends to be a reply or forward")
    elif seen != subject:
        subj_problems.append("has invisible characters")
    # A name in capitals is how that company writes itself, not shouting; anything else in capitals still is.
    own_name = getattr(getattr(contact, "company", None), "name", None)
    without_name = subject.replace(own_name.rstrip(" .,"), " ") if own_name else subject
    if re.search(r"\b[A-Z]{4,}\b", without_name) or "!" in subject:
        subj_problems.append("shouting (capitals or exclamation marks)")
    results.append(CheckResult("subject line", "fail" if subj_problems else "pass", "; ".join(subj_problems)))

    plain = _without_quotes(f"{subject}\n{main}")
    spam = sorted({m.group(0).lower() for m in SPAM_RE.finditer(plain)})
    results.append(CheckResult("spam words", "fail" if spam else "pass", ", ".join(spam)))
    claims = sorted({m.group(0).lower() for m in CLAIM_RE.finditer(plain)})
    results.append(CheckResult("unsupported claims", "fail" if claims else "pass",
                               ", ".join(claims) or "no results or endorsements claimed"))
    numbers = re.findall(r"\d[\d,.%]*", re.sub(r"\b\d{1,3}-minute\b", "", plain))  # our meeting length is not a claim
    results.append(CheckResult("numbers", "uncertain" if numbers else "pass",
                               f"check the source for: {', '.join(numbers[:5])}" if numbers else "none outside quotes"))

    # Every draft names the lead's own company, except the closing one: step 3, or an older one still waiting under its own subject.
    record = getattr(contact, "company", None)
    company = getattr(record, "name", None)
    if company and step != 3 and not subject.lower().startswith("closing the loop"):
        written = company_to_address(company, getattr(record, "website_title", None), getattr(record, "domain", None))
        named = bool(written) and _normalize(written) in _normalize(f"{subject}\n{main}")
        results.append(CheckResult("company name", "pass" if named else "uncertain",
                                   f"names {written}" if named else f"{company} is not named: is this the right company?"))

    sources = [_normalize(e.get("source_text") or e.get("fact") or "") for e in evidence or []]
    quotes = QUOTE_RE.findall(main)
    bad = [q for q in quotes if not any(_normalize(q.rstrip("…")) in s for s in sources)]
    if bad:
        results.append(CheckResult("personalization", "fail", f'quote not found in the evidence: "{bad[0][:60]}"'))
    elif step > 1 and not quotes:
        results.append(CheckResult("personalization", "pass", "follow-up to the earlier email; no new quote needed"))
    elif not evidence:
        results.append(CheckResult("personalization", "uncertain", "no recorded evidence behind this email"))
    elif not quotes:
        results.append(CheckResult("personalization", "uncertain", "no quoted evidence in the email"))
    else:
        urls = [e.get("url") for e in evidence if e.get("url")]
        results.append(CheckResult("personalization", "pass", f"quotes match the evidence ({urls[0] if urls else 'no link'})"))

    results.append(CheckResult("links", "fail" if LINK_RE.search(main) else "pass",
                               "no links in a first-touch email" if LINK_RE.search(main) else "none"))
    compliance = []
    if "unsubscribe" not in footer.lower():
        compliance.append("opt-out instruction missing")
    if "[postal address required" in footer:
        compliance.append("postal address not configured (sending stays blocked until it is)")
    results.append(CheckResult("compliance footer", "uncertain" if compliance else "pass", "; ".join(compliance)))

    recipient = []
    if contact is None or not contact.email:
        recipient.append("no email address")
    elif contact.email_status not in STATUS_OK:
        recipient.append(f"email status {contact.email_status}")
    if suppressed:
        recipient.append("on the do-not-contact list")
    results.append(CheckResult("recipient", "fail" if recipient else "pass",
                               "; ".join(recipient) or f"{contact.email} ({contact.email_status})"))

    prose = _without_quotes(main)
    results += [check_spelling("", _lowercase_words_only(f"{subject}\n{prose}")), check_grammar(subject, prose),
                check_cliches(subject, prose)]
    return build_report(results, subject, body, [])
