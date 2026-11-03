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


def injection_signals(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text or "")  # full-width letters and other look-alikes read as plain ones
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
