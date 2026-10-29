"""Reply drafts for people who contacted us. A human approves every message before it is sent."""

from __future__ import annotations

from aime.config import Settings
from aime.integrations.email_smtp import compliance_footer
from aime.knowledge.services import SERVICES


def _service_phrase(service_id: str | None) -> str:
    """The service in running text: "AI agent development", "custom CRM development" (acronyms keep their case)."""
    if service_id not in SERVICES:
        return "your project"
    return " ".join(w if sum(c.isupper() for c in w) > 1 else w.lower() for w in SERVICES[service_id].name.split())


def _linkedin_event(source_page: str | None) -> str:
    """The event's name when the person registered for a LinkedIn event ("LinkedIn event: <name>"), else ""."""
    page = source_page or ""
    return page.split(": ", 1)[1].strip() if page.startswith("LinkedIn event:") and ": " in page else ""


def draft_reply(settings: Settings, step: int, first_name: str | None, service_id: str | None,
                inquiry_excerpt: str | None, source_page: str | None = None) -> tuple[str, str]:
    name = first_name or "there"
    company = settings.company_name
    sender = settings.sender_name or f"The {company} team"
    phrase = _service_phrase(service_id)
    event = _linkedin_event(source_page)
    if step == 1 and (source_page or "").startswith("LinkedIn"):  # a LinkedIn lead form or event registration
        if event:
            subject = f"Thanks for registering: {event}"[:150]
            opening = f"Thank you for registering for {event} on LinkedIn."
        else:
            subject = f"Thanks for your interest in {phrase if service_id in SERVICES else company}"
            opening = f"Thank you for sharing your details with {company} on LinkedIn."
        body = (
            f"Hi {name},\n\n"
            f"{opening}\n\n"
            "So we can make the most of your time, could you tell me a little about:\n"
            "• the process or system you'd like to improve,\n"
            "• the tools your team uses today, and\n"
            "• your ideal timeline?\n\n"
            "If it's easier, reply with a few times that suit you for a short call.\n\n"
            f"Best regards,\n{sender}\n{company}"
        )
    elif step == 1:
        subject = f"Re: your inquiry about {phrase}"
        echo = f'\n\nYou mentioned: "{inquiry_excerpt.strip()[:240]}"' if inquiry_excerpt else ""
        body = (
            f"Hi {name},\n\n"
            f"Thank you for contacting {company} about {phrase}.{echo}\n\n"
            "So we can point you in the right direction, could you share a little more about:\n"
            "• the process or system you'd like to improve,\n"
            "• the tools your team uses today, and\n"
            "• your ideal timeline?\n\n"
            "If it's easier, reply with a few times that suit you for a short call.\n\n"
            f"Best regards,\n{sender}\n{company}"
        )
    elif step == 2:
        subject = (f"Following up: {event}"[:150] if event else f"Following up on your {phrase} inquiry"
                   if service_id in SERVICES else "Following up on your inquiry")
        about = f"your registration for {event}" if event else f"your recent inquiry about {phrase}"
        body = (
            f"Hi {name},\n\n"
            f"I wanted to follow up on {about}. "
            "If it's still a priority, I'm happy to set up a short call to understand what you need. "
            "If the timing isn't right, just let me know.\n\n"
            f"Best regards,\n{sender}\n{company}"
        )
    else:
        subject = "Closing the loop on your inquiry"
        body = (
            f"Hi {name},\n\n"
            "I haven't heard back, so I'll assume now isn't the right time and won't follow up again. "
            f"If anything changes, reply to this email and we'll pick it up from there.\n\n"
            f"Best regards,\n{sender}\n{company}"
        )
    return subject, body + compliance_footer(settings)
