"""Remove secrets and personal contact data from text before it is logged or stored as an error."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:access_token|refresh_token|client_secret|password|passphrase|api_key|apikey|token)\"?\s*[:=]\s*\"?)"
                r"[^\"&\s,}]{4,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"),
     "[REDACTED]"),
    (re.compile(r"\bAQ[A-Za-z0-9_-]{40,}\b"), "[REDACTED]"),  # LinkedIn access-token shape
    (re.compile(r"\bWPL_AP\d\.[A-Za-z0-9._+/=-]{8,}"), "[REDACTED]"),  # LinkedIn client-secret shape
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
]


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Applied to every log handler so secrets or emails never land in log files."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: leave the record alone
            return True
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        record.msg = redact(message)
        record.args = None
        return True
