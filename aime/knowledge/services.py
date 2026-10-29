"""The nine services we market, with keyword evidence used for requirement matching."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    pitch: str
    strong: tuple[str, ...]  # weight 2: specific evidence of the need
    weak: tuple[str, ...]  # weight 1: related but generic
    hashtags: tuple[str, ...]


SERVICES: dict[str, Service] = {
    s.id: s
    for s in [
        Service(
            "custom_crm",
            "Custom CRM Development",
            "CRMs shaped around how your team actually sells and serves customers",
            ("crm", "salesforce", "hubspot", "pipedrive", "zoho crm", "dynamics 365", "customer relationship",
             "pipeline management", "deal tracking", "contact management", "crm migration"),
            ("spreadsheet", "spreadsheets", "client database", "customer data", "sales pipeline"),
            ("#CRM", "#SalesOps", "#CustomSoftware"),
        ),
        Service(
            "ai_agents",
            "AI Agent Development",
            "AI agents that complete real tasks inside your systems, with human oversight",
            ("ai agent", "ai agents", "llm", "llms", "large language model", "gpt", "openai", "anthropic",
             "claude", "rag", "retrieval augmented", "copilot", "conversational ai", "chatbot", "genai",
             "generative ai"),
            ("ai", "assistant", "nlp", "prompt"),
            ("#AIAgents", "#ArtificialIntelligence", "#Automation"),
        ),
        Service(
            "agent_orchestration",
            "AI Agent Orchestration",
            "Coordinated multi-agent workflows with retries, approvals, and audit logs",
            ("multi-agent", "multi agent", "agent orchestration", "orchestration", "langchain", "langgraph",
             "crewai", "autogen", "agentic", "agent framework", "mcp"),
            ("workflow engine", "temporal", "airflow", "pipelines"),
            ("#AIAgents", "#AgenticAI", "#AIEngineering"),
        ),
        Service(
            "bpa",
            "Business Process Automation",
            "Automation that removes copy-paste work between your tools",
            ("automate", "automation", "automating", "manual process", "manual processes", "rpa", "zapier",
             "make.com", "n8n", "workato", "data entry", "back office", "back-office", "workflow automation"),
            ("workflow", "workflows", "operations", "integrations", "integration", "internal tools"),
            ("#BusinessAutomation", "#ProcessImprovement", "#Operations"),
        ),
        Service(
            "sales_automation",
            "Sales Automation",
            "Less admin for sales teams: automatic logging, quoting, and next steps",
            ("sales ops", "sales operations", "revops", "revenue operations", "salesloft", "outreach.io",
             "cpq", "quote to cash", "sales enablement", "sales automation", "gong", "sales follow-up",
             "sales follow-ups", "sales follow up", "sales follow ups", "sales followups"),
            ("sdr", "bdr", "account executive", "quota", "sales team"),
            ("#SalesAutomation", "#RevOps", "#B2BSales"),
        ),
        Service(
            "leadgen_automation",
            "Lead Generation Automation",
            "Capture, score, and route inbound leads fast and transparently",
            ("lead generation", "lead gen", "demand generation", "demand gen", "lead scoring",
             "marketing automation", "marketo", "pardot", "mql", "growth marketing", "inbound marketing",
             "lead qualification", "lead routing", "lead nurturing", "outbound prospecting"),
            ("leads", "funnel", "conversion", "growth"),
            ("#LeadGeneration", "#MarketingAutomation", "#B2BMarketing"),
        ),
        Service(
            "saas_dev",
            "SaaS Development",
            "Multi-tenant SaaS products built to scale without a rewrite",
            ("saas", "multi-tenant", "multitenant", "subscription billing", "b2b software", "self-serve",
             "mvp", "platform engineering"),
            ("platform", "product engineer", "startup", "product-led"),
            ("#SaaS", "#ProductDevelopment", "#SoftwareEngineering"),
        ),
        Service(
            "custom_software",
            "Custom Software Development",
            "Custom software that removes the bottleneck without replacing what works",
            ("custom software", "legacy", "modernization", "modernize", "rewrite", "internal tool",
             "web app", "mobile app", "full stack", "full-stack", "fullstack"),
            ("backend", "frontend", "react", "python", "node", "typescript", "api", "apis", "django", "rails"),
            ("#CustomSoftware", "#SoftwareDevelopment", "#DigitalTransformation"),
        ),
        Service(
            "ai_consulting",
            "AI Integration and Consulting",
            "Practical AI strategy and integration into the tools your team already uses",
            ("machine learning", "ml engineer", "data science", "ai strategy", "ai integration",
             "computer vision", "mlops", "fine-tuning", "fine tuning", "embeddings", "vector database"),
            ("data pipeline", "analytics", "data engineering", "data platform"),
            ("#AIStrategy", "#AIIntegration", "#ArtificialIntelligence"),
        ),
    ]
}


def service_name(service_id: str | None) -> str:
    if not service_id:
        return "—"
    svc = SERVICES.get(service_id)
    return svc.name if svc else service_id
