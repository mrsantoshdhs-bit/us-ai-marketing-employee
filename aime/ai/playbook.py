"""The objection playbook: for each kind of objection a prospect raises, the response points a draft may use and the
offers it may make. Owner-approved wording only: the conversation engine (aime.ai.conversation) writes nothing about
an objection that is not a point here, and an objection that is not here, or is switched off, goes to a person.

Stored in the database (kv_store "ai.playbook"), written by a Super Admin through `save` (/admin/settings/conversation),
with an audit event; over the code defaults below. The defaults state no fact about us: no client, result, number,
price, discount or date. They thank the prospect, and offer a call, a check-back or a pointer to the right person. A
point that names a price, a discount, a contract, a guarantee or legal matters is refused when it is saved, and every
draft built from the playbook still goes through the policy engine (aime.ai.policy) before a person sees it.

Offers a point may lead to (closed list): meeting (a call, by the booking link or by asking for times), check_back (we
write again when they say), referral (ask who the right person is), none (thank them and stop)."""

from __future__ import annotations

import copy
import re

from sqlalchemy.orm import Session

KV_KEY = "ai.playbook"
OFFERS = ("meeting", "check_back", "referral", "none")
LABELS = {"price": "Price or budget", "timing": "Timing (not now)", "competitor": "Already has a provider",
          "no_need": "No need", "authority": "Not the decision-maker", "trust": "Trust, references, case studies",
          "other": "Anything else"}
MAX_POINTS = 4

DEFAULT_PLAYBOOK: dict[str, dict] = {
    "price": {"enabled": True, "offers": ["meeting"],
              "points": ["Thank you for being open about the budget.",
                         "A short call with the Sales Manager is the fairest way to see whether this is worth it for you, "
                         "with no obligation."]},
    "timing": {"enabled": True, "offers": ["check_back"],
               "points": ["Thank you for letting me know, and no problem at all.",
                          "Would it help if I checked back at a better time? Just say when."]},
    "competitor": {"enabled": True, "offers": ["meeting"],
                   "points": ["That makes sense, and it is good to hear you already have help with this.",
                              "If it is ever useful to compare approaches, a short call is easy to arrange."]},
    "no_need": {"enabled": True, "offers": ["none"],
                "points": ["Thank you for the honest answer.", "If things change, just reply to this email."]},
    "authority": {"enabled": True, "offers": ["referral"],
                  "points": ["Thank you for letting me know.",
                             "Who would be the right person to speak with? You are welcome to forward this note."]},
    # References and case studies are the owner's own material: none is invented, so a person answers.
    "trust": {"enabled": False, "offers": ["none"], "points": ["Thank you, that is a fair question."]},
    "other": {"enabled": False, "offers": ["none"], "points": []},
}


def _clean(entry: dict, base: dict) -> dict:
    points = [str(p).strip()[:300] for p in (entry.get("points") if isinstance(entry.get("points"), list) else base["points"])
              if isinstance(p, str) and str(p).strip()][:MAX_POINTS]
    offers = [o for o in (entry.get("offers") if isinstance(entry.get("offers"), list) else base["offers"]) if o in OFFERS]
    return {"enabled": bool(entry.get("enabled", base["enabled"])), "points": points, "offers": offers or ["none"],
            "approved_by": entry.get("approved_by"), "approved_at": entry.get("approved_at")}


def load(session: Session | None) -> dict[str, dict]:
    """The playbook in effect: the stored one over the defaults, entry by entry. Each entry says whether the owner
    saved it (`approved_by`) or it is still the code's default wording."""
    stored: dict = {}
    if session is not None:
        from aime.db import kv_get

        try:
            stored = kv_get(session, KV_KEY) or {}
        except Exception:
            stored = {}
    out = {}
    for code, base in DEFAULT_PLAYBOOK.items():
        entry = stored.get(code) if isinstance(stored.get(code), dict) else {}
        out[code] = _clean(entry, base)
        out[code]["label"] = LABELS[code]
        out[code]["source"] = "owner" if entry else "default"
    return out


def problems(text: str) -> list[str]:
    """Why a point may not be in the playbook: the words the policy engine blocks in anything a prospect reads."""
    from aime.ai.claims import DEFAULT_PROHIBITED_TOPICS
    from aime.ai.guard import hidden_characters, plain
    from aime.automation import SENSITIVE

    out = []
    hidden = hidden_characters(text)
    if hidden:
        out.append(f"characters a reader cannot see ({', '.join(hidden[:3])})")
    text = plain(text)  # look-alike letters and invisible characters hide no word
    m = SENSITIVE.search(text or "")
    if m:
        out.append(f'"{m.group(0)}"')
    for label, words in DEFAULT_PROHIBITED_TOPICS.values():
        hit = re.search(words, text or "", re.I)
        if hit:
            out.append(f'{label} ("{hit.group(0)[:30]}")')
    if re.search(r"\d", text or ""):
        out.append("a number (no figure is stated for us in a draft)")
    return list(dict.fromkeys(out))


def save(session: Session, playbook: dict[str, dict], actor: str) -> list[str]:
    """Store the owner's playbook, replacing the stored one, audited. Raises ValueError, naming every refused point,
    when a point names a price, discount, contract, guarantee, legal matter or a number."""
    from aime.automation import audit
    from aime.db import kv_set
    from aime.timeutil import utcnow

    refused = []
    clean: dict[str, dict] = {}
    for code, base in DEFAULT_PLAYBOOK.items():
        entry = _clean(playbook.get(code) or {}, base)
        for p in entry["points"]:
            why = problems(p)
            if why:
                refused.append(f"{LABELS[code]}: \"{p[:80]}\" mentions {', '.join(why[:2])}")
        entry["approved_by"], entry["approved_at"] = (actor or "")[:80], utcnow().isoformat(timespec="seconds")
        clean[code] = entry
    if refused:
        raise ValueError("; ".join(refused))
    before = load(session)
    kv_set(session, KV_KEY, copy.deepcopy(clean))
    changed = [LABELS[c] for c in clean if (before[c]["points"], before[c]["offers"], before[c]["enabled"])
               != (clean[c]["points"], clean[c]["offers"], clean[c]["enabled"])]
    audit(session, actor, "ai_playbook_saved", KV_KEY, ("changed: " + ", ".join(changed)) if changed else "saved, unchanged")
    return changed
