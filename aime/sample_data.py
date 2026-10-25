"""Clearly fictional sample data for tests and the demo database.

Every record uses reserved `.example` domains and is flagged is_sample=True so it can never be
mistaken for a real prospect. Nothing here is ever contacted.
"""

from __future__ import annotations

SAMPLE_HN_THREAD = {"objectID": "90000001", "title": "Ask HN: Who is hiring? (October 2026)"}

SAMPLE_HN_ITEMS = [
    {
        "id": 90000011, "author": "sample_northwind", "created_at": "2026-10-01T15:05:00.000Z",
        "text": ("Northwind Analytics | Senior Backend Engineer | Denver, CO | ONSITE | "
                 "<a href=\"https:&#x2F;&#x2F;northwind-analytics.example\">https:&#x2F;&#x2F;northwind-analytics.example</a>"
                 "<p>We are a B2B SaaS platform for revenue teams. You will automate manual processes, build "
                 "Salesforce and HubSpot integrations, and ship AI agents (LLM, RAG) for our customers. "
                 "Apply: jobs@northwind-analytics.example or call 303-555-0100.</p>"),
    },
    {
        "id": 90000012, "author": "sample_lumen", "created_at": "2026-10-01T15:06:00.000Z",
        "text": ("Lumen Home Loans | Full-stack Engineer | Remote (US) | "
                 "<a href=\"https:&#x2F;&#x2F;jobs.lever.co&#x2F;lumen\">apply</a>"
                 "<p>Mortgage and real estate fintech. We are modernizing a legacy loan workflow and "
                 "replacing spreadsheets with a custom CRM and workflow automation.</p>"),
    },
    {
        "id": 90000013, "author": "sample_thames", "created_at": "2026-10-01T15:07:00.000Z",
        "text": ("Thames Ledger | Data Engineer | London, UK | HYBRID"
                 "<p>Accounting software for UK firms; machine learning and automation roles.</p>"),
    },
    {
        "id": 90000014, "author": "sample_devshop", "created_at": "2026-10-01T15:08:00.000Z",
        "text": ("Acme Dev Shop | React Developer | Austin, TX | "
                 "<a href=\"https:&#x2F;&#x2F;acme-devshop.example\">acme-devshop.example</a>"
                 "<p>We are a software development agency: we build software for our clients, including "
                 "automation and AI agents.</p>"),
    },
    {
        "id": 90000015, "author": "sample_vague", "created_at": "2026-10-01T15:09:00.000Z",
        "text": "Quiet Co | Engineer | Remote<p>We build things.</p>",
    },
    {
        "id": 90000016, "author": "sample_northwind2", "created_at": "2026-10-01T15:10:00.000Z",
        "text": ("Northwind Analytics Inc. | Product Designer | Denver, CO | "
                 "<a href=\"https:&#x2F;&#x2F;www.northwind-analytics.example&#x2F;careers\">careers</a>"
                 "<p>Help design our revenue operations product and sales automation workflows.</p>"),
    },
]

SAMPLE_INBOUND = [
    {
        "first_name": "Dana", "last_name": "Sample", "email": "dana@brightpath-realty.example",
        "company": "Brightpath Realty Group", "website": "https://brightpath-realty.example",
        "state": "TX", "city": "Austin", "country": "US", "industry": "real_estate",
        "message": ("Our agents track leads in spreadsheets and follow-ups get missed. We'd like a custom CRM "
                    "that captures leads from our website and automates follow-up reminders."),
        "services_interested": ["custom_crm", "sales_automation"], "timeline": "Next quarter",
        "consent_marketing": False, "source_page": "/contact", "is_sample": True,
    },
    {
        "first_name": "Riley", "last_name": "Example", "email": "riley@pixelpine.example",
        "company": "Pixel & Pine Agency", "state": "Oregon", "city": "Portland", "country": "USA",
        "message": "We spend days building client reports by hand. Can you automate reporting with AI?",
        "services_interested": "Business Process Automation; AI Agent Development",
        "consent_marketing": True, "is_sample": True,
    },
    {
        "name": "Spam Bot", "email": "bot@spam.example", "company": "Cheap SEO",
        "message": "Buy links http://a http://b http://c http://d http://e http://f", "hp": "filled",
        "is_sample": True,
    },
    {
        "first_name": "Jamie", "email": "jamie@maple-north.example", "company": "Maple North Consulting",
        "country": "Canada", "message": "Looking for AI integration consulting for our Toronto office.",
        "is_sample": True,
    },
]
