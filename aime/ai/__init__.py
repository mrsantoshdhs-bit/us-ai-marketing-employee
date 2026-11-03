"""The AI reasoning core (Option B, Phase 2): shadow only.

AI proposes, policy validates, an authorized executor acts, the result is audited. In this phase nothing here acts:

- aime.ai.schemas   the typed answers a model may give (a reply analysis, and proposals);
- aime.ai.prompts   versioned prompt templates; the version is stored with every decision;
- aime.ai.tools     the tools a model may call: read-only CRM queries, scoped to one lead, and proposal tools that
                    only record a proposal; no tool sends, books, publishes or changes a setting;
- aime.ai.claims    what may be said about us (approved claims) and what may never be (prohibited topics);
- aime.ai.policy    the policy engine that routes every proposal to AUTO_INTERNAL, HUMAN_REVIEW or BLOCK, with the
                    rule-based reply classifier (aime.reply_intel) as a safety floor that always wins;
- aime.ai.state     the per-lead conversation state machine;
- aime.ai.escalation when a person must take over;
- aime.ai.decisions the AI decision log (table ai_decisions);
- aime.ai.engine    the shadow analysis of a reply that ties these together.

Nothing in this package is called by a scheduled job yet: no customer sees any of it."""
