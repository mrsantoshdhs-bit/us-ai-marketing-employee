"""The agent responsibility and permission matrix (settings section "agents"): for each agent what it is responsible for,
the tools it may use and the actions it must never take.

This is data and display only. What is enforced today is unchanged: an agent switched off on the AI Agents page is skipped
(aime.agents.base.agent_enabled), and the safety floor in code (approvals, suppression, the env master switches) applies to
every agent. The AI core reads `agent_policy(name)` to enforce tool permissions later.
"""

from __future__ import annotations

# Never allowed to any agent, whatever its own row says: these are the safety floor and the env-only switches.
COMMON_PROHIBITED = [
    "Send an email without a person's approval or an active campaign authorization",
    "Publish a post without a person's approval",
    "Change REQUIRE_HUMAN_APPROVAL, OUTBOUND_EMAIL_ENABLED or PUBLISHING_ENABLED",
    "Contact anyone on the suppression list or who opted out",
    "Raise a sending limit",
    "State prices, discounts or contract terms",
    "Scrape LinkedIn or any site that forbids it; buy or use data without the owner's approval",
]

TOOLS = {
    "marketing_manager": ["read CRM statistics", "write marketing plans"],
    "research": ["public sources (HN hiring API, SEC EDGAR)", "your CSV imports", "CRM: companies, signals, leads"],
    "company_research": ["company websites (robots.txt honoured)", "SEC filings", "CRM: research snapshots, contacts"],
    "linkedin_content": ["local language model or templates", "content calendar"],
    "publishing": ["LinkedIn official API (approved posts only)"],
    "qualification": ["CRM: leads and scores"],
    "sales_workflow": ["email drafts", "sending (approved emails only, within the limits)", "CRM: follow-ups, tasks"],
    "inbox_monitor": ["sales mailbox (read-only)", "CRM: replies, contacts, suppressions"],
    "crm": ["website checks", "CRM hygiene: dedupe, lifecycle"],
    "analytics": ["CRM statistics", "reports"],
}


def default_matrix() -> dict:
    from aime.agents import AGENTS

    return {name: {"responsibilities": list(cls.responsibilities), "allowed_tools": list(TOOLS.get(name, [])),
                   "prohibited_actions": list(COMMON_PROHIBITED)} for name, cls in AGENTS.items()}


def agent_policy(name: str, ctx=None) -> dict:
    """What an agent is responsible for, may use and must not do, and whether it is switched on now. Raises KeyError for
    an unknown agent. The prohibited actions always include the common safety floor."""
    from aime import settings_store
    from aime.agents import AGENTS
    from aime.agents.base import agent_enabled
    from aime.context import get_context

    if name not in AGENTS:
        raise KeyError(f"Unknown agent {name!r}")
    ctx = ctx or get_context()
    row = settings_store.get(ctx.settings, "agents")["agents"].get(name) or default_matrix()[name]
    prohibited = list(dict.fromkeys([*COMMON_PROHIBITED, *row.get("prohibited_actions", [])]))
    return {"name": name, "title": AGENTS[name].title, "enabled": agent_enabled(ctx, name),
            "responsibilities": list(row.get("responsibilities", [])), "allowed_tools": list(row.get("allowed_tools", [])),
            "prohibited_actions": prohibited}
