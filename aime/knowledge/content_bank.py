"""Curated LinkedIn post angles per service.

Placeholders: {audience} (industry audience label) and {pain} (an industry pain point).
The copy deliberately avoids statistics, client names, and promised results.
"""

from __future__ import annotations

ANGLES: dict[str, list[dict]] = {
    "custom_crm": [
        {
            "title": "When an off-the-shelf CRM stops fitting your process",
            "hook": "Most {audience} don't have a CRM problem. They have a process the CRM was never designed around.",
            "insight": "When the tool doesn't match how deals actually move, people work around it: side spreadsheets, sticky notes, and {pain}. The data gets thinner, and so does trust in the pipeline.",
            "bullets": [
                "Model the pipeline stages your team really uses, not a vendor's default",
                "Capture data automatically from forms, email, and calls instead of asking people to re-type it",
                "Build the reports leadership asks for into the system itself",
            ],
            "closing": "A custom CRM isn't about more features. It's about fewer workarounds.",
        },
        {
            "title": "Signs your team has outgrown its CRM",
            "hook": "If your team keeps a spreadsheet open next to the CRM, the CRM has already lost.",
            "insight": "It shows up again and again for {audience}: {pain}. Each workaround seems small, but together they hide where deals stall.",
            "bullets": [
                "Fields nobody fills in because they don't fit the workflow",
                "Reports rebuilt by hand every week",
                "Handoffs between teams that happen in chat instead of in the system",
            ],
            "closing": "Fixing this usually starts with mapping the real process, then shaping the software to it.",
        },
    ],
    "ai_agents": [
        {
            "title": "AI agents that do real work, not just chat",
            "hook": "An AI agent is only useful if it can finish a task, not just talk about it.",
            "insight": "For {audience}, the best first agents are narrow. They read a request, pull the right data, take an action in your systems, and hand off to a person when judgment is needed. A common place to start: {pain}.",
            "bullets": [
                "Start with one repetitive, rules-heavy task",
                "Connect the agent to the systems where the work actually lives",
                "Keep a human approval step until the results are proven",
            ],
            "closing": "Small, reliable agents beat ambitious demos that never reach production.",
        },
        {
            "title": "Where AI agents fit in a growing business",
            "hook": "The question isn't whether to use AI agents. It's which task to hand over first.",
            "insight": "Look for work that is frequent, well defined, and slowed down by copying information between tools. For many {audience}, that means {pain}.",
            "bullets": [
                "Triage and route incoming requests",
                "Draft responses and documents for human review",
                "Keep records updated across systems automatically",
            ],
            "closing": "Pick one workflow, measure it honestly, and expand from there.",
        },
    ],
    "agent_orchestration": [
        {
            "title": "One agent is a tool. Several agents need a manager.",
            "hook": "Once you run more than one AI agent, coordination becomes the real engineering problem.",
            "insight": "A research agent, a drafting agent, and a CRM agent can each work fine alone. Together they need shared context, clear handoffs, retries when something fails, and logs you can audit. Without that, {pain} simply moves into a new system.",
            "bullets": [
                "Define each agent's inputs, outputs, and boundaries",
                "Add an orchestration layer that handles retries, errors, and approvals",
                "Log every step so people can see what happened and why",
            ],
            "closing": "Orchestration is what turns a set of clever agents into a dependable workflow.",
        },
        {
            "title": "Designing multi-agent workflows people can trust",
            "hook": "Trust in AI workflows comes from visibility, not from bigger models.",
            "insight": "For {audience}, a multi-agent system should behave like a well-run team: every task has an owner, every handoff is recorded, and anything uncertain is escalated to a person.",
            "bullets": [
                "Explicit contracts between agents",
                "Human checkpoints for high-impact actions",
                "Monitoring that flags failures before customers notice",
            ],
            "closing": "Build the guardrails first, and the automation scales safely.",
        },
    ],
    "bpa": [
        {
            "title": "The hidden cost of copy-paste work",
            "hook": "Every manual handoff between two systems is a place where work waits and mistakes creep in.",
            "insight": "For {audience}, it often looks like {pain}. None of it is anyone's main job, which is exactly why it never gets fixed.",
            "bullets": [
                "List the tasks people repeat every day or week",
                "Find where data is re-typed from one tool into another",
                "Automate the handoff, and keep a person on the exceptions",
            ],
            "closing": "Automation works best when it removes the boring steps and leaves the judgment calls to people.",
        },
        {
            "title": "Start automation with the process, not the tool",
            "hook": "Buying an automation tool before mapping the process is like buying furniture before measuring the room.",
            "insight": "The fastest wins for {audience} usually come from writing down how work flows today, including {pain}, and only then choosing what to automate.",
            "bullets": [
                "Map the current process step by step",
                "Mark the steps that follow clear rules",
                "Automate those first and track the time it gives back to the team",
            ],
            "closing": "Clarity first, code second.",
        },
    ],
    "sales_automation": [
        {
            "title": "Give sales reps their selling time back",
            "hook": "Sales reps rarely complain about selling. They complain about everything around it.",
            "insight": "For {audience}, the drag usually comes from {pain}. Sales automation should remove that admin work without making the pipeline harder to read.",
            "bullets": [
                "Log calls, emails, and meetings to the CRM automatically",
                "Generate quotes and proposals from approved templates",
                "Trigger the next step when a deal changes stage",
            ],
            "closing": "The goal isn't more activity. It's more time for conversations that matter.",
        },
        {
            "title": "Automate the follow-up, not the relationship",
            "hook": "Automation should make follow-up more consistent, not more robotic.",
            "insight": "Deals slip when the next step depends on someone remembering it. For {audience}, a good system schedules the follow-up, drafts it, and lets a person decide what actually gets sent.",
            "bullets": [
                "Reminders tied to deal stages",
                "Drafts that a rep reviews before sending",
                "Clear opt-out handling and respected contact preferences",
            ],
            "closing": "Consistency builds trust. Spam destroys it.",
        },
    ],
    "leadgen_automation": [
        {
            "title": "Lead generation that respects the buyer",
            "hook": "More leads isn't the goal. Better-qualified conversations are.",
            "insight": "Many {audience} collect leads from several channels and then lose track of them among {pain}. Automating capture, scoring, and routing keeps the best opportunities from going cold.",
            "bullets": [
                "Capture every inbound inquiry in one place",
                "Score leads on fit and stated needs, not guesswork",
                "Route qualified leads to the right person quickly",
            ],
            "closing": "Speed and relevance beat volume.",
        },
        {
            "title": "Score leads on evidence, not hope",
            "hook": "A lead score is only as honest as the evidence behind it.",
            "insight": "For {audience}, useful scoring combines company fit, the needs a prospect has actually expressed, and how they reached you. Anything else is a guess dressed up as a number.",
            "bullets": [
                "Define fit criteria together with your sales team",
                "Use only signals you can trace back to a source",
                "Review and recalibrate the scoring regularly",
            ],
            "closing": "Transparent scoring means sales and marketing argue less and close more.",
        },
    ],
    "saas_dev": [
        {
            "title": "From internal tool to SaaS product",
            "hook": "Some of the best SaaS products start as an internal tool someone else wanted to buy.",
            "insight": "When {audience} have built a process that works, turning it into a product means multi-tenant architecture, billing, onboarding, and security that holds up under real customers.",
            "bullets": [
                "Validate the core workflow with a small group of users",
                "Design for multiple tenants and roles from day one",
                "Automate onboarding so growth doesn't depend on manual setup",
            ],
            "closing": "A focused first version on solid foundations beats a feature-heavy launch.",
        },
        {
            "title": "Building SaaS that scales without a rewrite",
            "hook": "The expensive part of SaaS isn't version one. It's rewriting version one.",
            "insight": "Early decisions about data models, permissions, and integrations decide how painful growth will be. For {audience}, it pays to get those right before the user base arrives.",
            "bullets": [
                "Clear tenant isolation and access control",
                "An API-first design so integrations come easily",
                "Observability built in, not bolted on",
            ],
            "closing": "Build the boring parts well and the exciting parts get easier.",
        },
    ],
    "custom_software": [
        {
            "title": "When off-the-shelf software becomes the bottleneck",
            "hook": "Off-the-shelf software is great until your process becomes your competitive advantage.",
            "insight": "For {audience}, the warning signs are familiar: {pain}, extra subscriptions to patch gaps, and integrations that break quietly. Custom software makes sense when the workflow itself is what sets you apart.",
            "bullets": [
                "Keep the tools that work and replace only the bottleneck",
                "Integrate with existing systems instead of starting over",
                "Ship in small increments so the team sees value early",
            ],
            "closing": "The right build is the smallest one that removes the constraint.",
        },
        {
            "title": "Modernizing legacy systems without stopping the business",
            "hook": "Replacing a legacy system shouldn't mean freezing the business for a year.",
            "insight": "Many {audience} rely on software that still works but blocks every new idea. A staged modernization replaces one piece at a time while the old system keeps running.",
            "bullets": [
                "Wrap the legacy system with a clean API",
                "Move one workflow at a time to the new platform",
                "Retire old components only after the new ones are proven",
            ],
            "closing": "Steady progress beats a risky big-bang launch.",
        },
    ],
    "ai_consulting": [
        {
            "title": "A practical first step into AI",
            "hook": "Most AI projects stall because they start with the model instead of the problem.",
            "insight": "For {audience}, a better starting point is a short list of business problems, the data you already have, and an honest look at where AI helps and where simple automation is enough. Often the first target is {pain}.",
            "bullets": [
                "Rank use cases by business value and data readiness",
                "Prototype with real data before committing budget",
                "Plan for security, privacy, and human oversight from the start",
            ],
            "closing": "Good AI consulting should sometimes tell you not to use AI.",
        },
        {
            "title": "Bringing AI into the tools your team already uses",
            "hook": "The best AI feature is the one your team doesn't have to switch apps to use.",
            "insight": "Rather than adding yet another platform, {audience} usually get more value by bringing AI into the CRM, help desk, or document tools they rely on every day.",
            "bullets": [
                "Summaries and drafts inside existing workflows",
                "Search across your own documents and records",
                "Clear controls over what data the AI can access",
            ],
            "closing": "Meet people where they work and adoption follows.",
        },
    ],
}

BULLET_INTROS = ["What tends to help:", "A practical approach:", "Where to start:"]

CTAS = [
    "What's one process your team would happily never do by hand again?",
    "Where does work get stuck in your team's week?",
    "Which part of your workflow would you automate first?",
    "How is your team handling this today?",
    "What would you add to this list?",
]
