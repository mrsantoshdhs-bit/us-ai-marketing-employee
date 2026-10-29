"""Which of our services a request asks for. A generic "automate" is business process automation only when the text
names nothing more specific: in the 29 Oct pilot, "lead generation automation" and "sales automation" requests were
labelled Business Process Automation, because "automation" scored as much as "lead generation" and the tie went to
the alphabetically first service."""

import pytest

from aime.matching import match_services


def ids(text):
    return [m.service_id for m in match_services(text)]


@pytest.mark.parametrize(("text", "expected"), [
    # the pilot's requests
    ("We want lead generation automation for our marketing agency clients.", ["leadgen_automation"]),
    ("Our sales reps spend too long on admin; interested in sales automation.", ["sales_automation"]),
    ("We need to automate our sales follow-ups and pipeline updates", ["sales_automation"]),
    ("Looking for help with outbound prospecting and lead scoring", ["leadgen_automation"]),
    ("We want marketing automation for our inbound funnel", ["leadgen_automation"]),
    ("We want to automate our lead generation workflow", ["leadgen_automation"]),
    # back-office automation is still recognised, alone or next to another need
    ("Manual data entry between our order system and invoicing takes hours; looking to automate it.", ["bpa"]),
    ("We would like to automate our business processes", ["bpa"]),
    ("We need workflow automation with Zapier", ["bpa"]),
    ("We need lead generation, and to automate our invoice data entry", ["bpa", "leadgen_automation"]),
])
def test_a_specific_need_wins_over_a_generic_automate(text, expected):
    assert sorted(ids(text)) == sorted(expected)


def test_other_services_are_unchanged():
    assert ids("We need AI agents to answer patient inquiries and book appointments.") == ["ai_agents"]
    assert ids("Our agents lose leads between spreadsheets; we want a custom CRM with automatic follow-up reminders.") \
        == ["custom_crm"]
    found = ids("We need a Salesforce CRM migration and to automate manual processes")
    assert "custom_crm" in found and "bpa" in found  # "manual processes" is specific back-office evidence
    assert match_services("We use python and react") == []


def test_each_match_says_how_sure_it_is_and_why():
    lead_gen = match_services("We want lead generation automation")[0]
    assert (lead_gen.service_id, lead_gen.confidence) == ("leadgen_automation", "medium")
    assert lead_gen.explain() == "Lead Generation Automation (medium): mentions lead generation"
    assert match_services("Lead generation and lead scoring, please")[0].confidence == "high"
    assert match_services("We would like to automate our business processes")[0].confidence == "low"
    assert match_services("Manual data entry; we want to automate it")[0].confidence == "high"
