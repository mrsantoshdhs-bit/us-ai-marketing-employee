"""The AI employee team."""

from aime.agents.analytics import AnalyticsReportingAgent
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.crm import CRMManagementAgent
from aime.agents.linkedin_content import LinkedInContentAgent
from aime.agents.marketing_manager import MarketingManagerAgent
from aime.agents.publishing import AuthorizedPublishingAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.research import USBusinessResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent

AGENTS = {
    cls.name: cls
    for cls in (
        MarketingManagerAgent,
        USBusinessResearchAgent,
        CompanyResearchAgent,
        LinkedInContentAgent,
        AuthorizedPublishingAgent,
        LeadQualificationAgent,
        SalesWorkflowAgent,
        CRMManagementAgent,
        AnalyticsReportingAgent,
    )
}

__all__ = ["AGENTS"] + [cls.__name__ for cls in AGENTS.values()]
