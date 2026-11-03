"""Two readings of a prospect's text that need no model: the rule floor and the prompt-injection signals.

The rule floor is the existing reply classifier (aime.reply_intel.classify) run on the text as it stands, unchanged:
opt-outs, complaints, legal threats and bounces are decided by those rules and always win over any model. `Floor.hit`
says the rules found one; the policy engine then lets no external AI proposal through.

Prompt-injection signals are phrases that try to speak to the model instead of to us ("ignore previous instructions",
"you are now", "system prompt", "reply with a 50% discount"). They do not decide anything by themselves: the prospect's
text is always quoted as data, and whatever the model then proposes is checked on its own. A signal makes the
conversation go to a person, and its words are logged as a reason."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

STOP_CATEGORIES = ("unsubscribe", "bounce")

INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.!?\n]{0,40}\b(previous|prior|above|earlier|all|your|the|any)\b[^.!?\n]{0,25}"
    r"\b(instructions?|rules?|prompts?|guidelines?|policy|policies|directions?)\b"
    r"|\b(you are|you're|act as|pretend (to be|you are)|roleplay as|from now on you)\b[^.!?\n]{0,40}\b(assistant|ai|bot|model|gpt|"
    r"llm|admin|developer|system|dan|unrestricted|jailbroken)\b"
    r"|\b(system|developer|hidden|original) (prompt|message|instructions?)\b"
    r"|\b(new|updated|real) instructions?\b|\binstructions? (for|to) (the )?(ai|assistant|model|bot)\b"
    r"|\[\s*(system|assistant|inst)\s*\]|<\|?(system|im_start|assistant)\|?>|###\s*(system|instruction)"
    r"|\b(reply|respond|answer|write|tell (me|us|them)|say)\b[^.!?\n]{0,30}\b(with|that)\b[^.!?\n]{0,40}\b(\d+\s?%|discount|free|"
    r"guarantee\w*|price|contract)\b"
    r"|\b(send|forward|email|mail)\b[^.!?\n]{0,30}\b(all|every|the)\b[^.!?\n]{0,20}\b(leads?|contacts?|customers?|clients?|data|"
    r"database|list|emails?|passwords?|keys?)\b[^.!?\n]{0,20}\b(to|at)\b"
    r"|\b(reveal|print|show|output|repeat)\b[^.!?\n]{0,30}\b(prompt|instructions?|config\w*|settings|api key|password)\b"
    r"|\b(change|update|set|turn off|disable)\b[^.!?\n]{0,30}\b(settings?|approval|safety|filter|suppression|opt[- ]?out)\b",
    re.I)


# Characters a reader does not see: zero-width spaces and joiners, the soft hyphen, bidi controls (Unicode category Cf),
# and the blank "filler" letters. "dis<U+200B>count" reads as "discount" in a mail client, but not to a regular expression.
_FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# Cyrillic and Greek letters drawn like Latin ones ("di<U+0455>count" with a Cyrillic s), read as the Latin letter for
# the checks only. NFKC folds full-width forms, not these.
_LOOKALIKE = ("\u0430a \u0435e \u043eo \u0440p \u0441c \u0443y \u0445x \u0456i \u0458j \u0455s \u0501d \u051bq \u051dw "
              "\u04bbh \u04cfl \u043ak \u043cm \u0442t \u0432b \u043dh \u0433r "
              "\u0410A \u0412B \u0415E \u041aK \u041cM \u041dH \u041eO \u0420P \u0421C \u0422T \u0425X \u0406I \u0408J "
              "\u0405S \u04aeY "
              "\u03b1a \u03b5e \u03b9i \u03bak \u03bdv \u03bfo \u03c1p \u03c4t \u03c5u \u03c7x \u0391A \u0392B \u0395E \u0396Z "
              "\u0397H \u0399I \u039aK \u039cM \u039dN \u039fO \u03a1P \u03a4T \u03a5Y \u03a7X")
CONFUSABLES = str.maketrans({pair[0]: pair[1] for pair in _LOOKALIKE.split()})


def hidden_characters(text: str) -> list[str]:
    """The invisible characters in `text`, as U+XXXX: a person approving a draft cannot see them."""
    return list(dict.fromkeys(f"U+{ord(ch):04X}" for ch in text or "" if unicodedata.category(ch) == "Cf" or ch in _FILLERS))


def plain(text: str) -> str:
    """The text as a reader sees it, for the word checks (prices, contracts, injected instructions): full-width forms
    folded (NFKC), invisible characters taken out, Cyrillic and Greek look-alike letters read as Latin ones."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf" and ch not in _FILLERS)
    return text.translate(CONFUSABLES)


def injection_signals(text: str) -> list[str]:
    text = plain(text)  # full-width letters, invisible characters and look-alike letters read as plain ones
    return [m.group(0)[:120] for m in INJECTION.finditer(text)][:5]


@dataclass
class Floor:
    category: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    intent: str | None = None
    complaint: bool = False
    legal: bool = False

    @property
    def hit(self) -> bool:
        """The rules read an opt-out, a complaint or a bounce: no AI message may go to this person."""
        return self.category in STOP_CATEGORIES or self.complaint

    @property
    def as_intent(self) -> str:
        """The floor's reading in the AI's label set (aime.ai.schemas.INTENTS)."""
        if self.complaint:
            return "complaint"
        if self.category == "bounce":
            return "out_of_office"  # an automatic message; no person wrote it
        if self.category in ("calendly",):
            return "interested"
        return self.category


def rule_floor(text: str, subject: str = "", body: str | None = None, auto_submitted: bool = False) -> Floor:
    """The existing classifier's reading of one reply, on an unsaved row: nothing is written."""
    from aime.models import InboundEmail
    from aime.reply_intel import COMPLAINT, LEGAL_MENTION, classify, intent_of

    row = InboundEmail(subject=subject or "", body_text=body if body is not None else text, new_text=text,
                       from_email="prospect@example.invalid", auto_submitted=auto_submitted)
    category, confidence, reasons = classify(row)
    row.category = category
    said = f"{subject}\n{text}"
    complaint = bool(COMPLAINT.search(said)) and category in ("unsubscribe", "not_interested", "other", "objection")
    return Floor(category, confidence, list(reasons), intent_of(row), complaint, bool(LEGAL_MENTION.search(said)))
