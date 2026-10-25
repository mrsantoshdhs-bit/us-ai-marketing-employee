"""Target industries with classification keywords, public-data codes, and pain points."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Industry:
    id: str
    label: str
    audience: str  # used mid-sentence in content
    keywords: tuple[str, ...]
    naics: tuple[str, ...]  # Census NAICS2017 codes for market sizing
    sic: tuple[str, ...]  # SEC EDGAR SIC codes
    pains: tuple[str, ...]
    hashtags: tuple[str, ...]


INDUSTRIES: dict[str, Industry] = {
    i.id: i
    for i in [
        Industry(
            "saas", "SaaS", "SaaS companies",
            ("saas", "software as a service", "b2b software", "subscription software", "cloud platform"),
            ("5112",), ("7372",),
            ("trial-to-paid handoffs that live in spreadsheets",
             "support, sales, and product data split across different tools",
             "manual onboarding steps that slow down time-to-value"),
            ("#SaaS",),
        ),
        Industry(
            "real_estate", "Real Estate", "real estate teams",
            ("real estate", "proptech", "property management", "brokerage", "realtor", "mortgage",
             "homebuying", "rental", "commercial real estate"),
            ("531",), ("6531", "6798"),
            ("leads from listings, referrals, and open houses landing in different inboxes",
             "follow-ups that depend on an agent's memory",
             "transaction paperwork re-keyed into several systems"),
            ("#RealEstate", "#PropTech"),
        ),
        Industry(
            "marketing_agencies", "Marketing Agencies", "marketing agencies",
            ("marketing agency", "digital agency", "advertising agency", "creative agency", "media agency",
             "seo agency", "performance marketing", "agency", "client reports", "client reporting"),
            ("5418",), ("7311",),
            ("client reports assembled by hand every month",
             "new-business leads tracked in a shared spreadsheet",
             "campaign data copied between ad platforms and dashboards"),
            ("#DigitalMarketing", "#AgencyLife"),
        ),
        Industry(
            "professional_services", "Professional Services", "professional services firms",
            ("law firm", "legal", "accounting", "cpa", "consulting firm", "advisory", "tax", "audit",
             "legaltech", "staffing"),
            ("5411", "5412", "5416"), ("8111", "8721", "8742"),
            ("intake forms re-typed into practice management tools",
             "proposals and engagement letters built from scratch",
             "billable time lost to administrative work"),
            ("#ProfessionalServices",),
        ),
        Industry(
            "b2b_sales", "B2B Sales", "B2B sales teams",
            ("b2b sales", "distributor", "wholesale", "manufacturer", "sales-led", "enterprise sales",
             "field sales", "channel partners"),
            ("423",), ("5045", "5065", "5084"),
            ("reps spending hours on CRM data entry",
             "quotes assembled manually from price sheets",
             "pipeline reports that are out of date by the time they are shared"),
            ("#B2BSales",),
        ),
        Industry(
            "tech_startups", "Technology Startups", "tech startups",
            ("startup", "seed", "series a", "series b", "yc", "y combinator", "venture-backed", "founding"),
            ("5415",), ("7370", "7371", "7373", "7374"),
            ("founders doing sales operations late at night",
             "internal tools held together with scripts",
             "AI prototypes that never make it to production"),
            ("#Startups",),
        ),
        Industry(
            "ecommerce", "E-commerce", "e-commerce brands",
            ("e-commerce", "ecommerce", "shopify", "dtc", "direct-to-consumer", "online store", "retail",
             "marketplace", "amazon seller"),
            ("4541",), ("5961",),
            ("order exceptions handled with copy and paste",
             "customer questions answered one ticket at a time",
             "inventory and marketing data that never quite line up"),
            ("#Ecommerce",),
        ),
        Industry(
            "smb", "Small and Medium Businesses", "small and mid-sized businesses",
            ("small business", "smb", "smbs", "local business", "family-owned", "mid-sized", "midsize"),
            (), (),
            ("owners answering the same customer questions every day",
             "quotes, invoices, and follow-ups handled manually",
             "data scattered across email, spreadsheets, and apps"),
            ("#SmallBusiness",),
        ),
    ]
}


def industry_label(industry_id: str | None) -> str:
    if not industry_id:
        return "—"
    ind = INDUSTRIES.get(industry_id)
    return ind.label if ind else industry_id
