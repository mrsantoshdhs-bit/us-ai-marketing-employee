from aime.dedupe import domain_from_email, domain_from_url, find_duplicate, normalize_name
from aime.knowledge.geo import STATE_PRIORITY, STATES, normalize_state, parse_location
from aime.matching import classify_industry, detect_competitor, match_services
from aime.models import Company


def test_all_50_states_covered():
    assert len(STATES) == 50
    assert sorted(STATE_PRIORITY) == sorted(STATES)


def test_parse_location_us_and_non_us():
    loc = parse_location("Acme | Engineer | San Francisco, CA | ONSITE")
    assert loc.is_us and loc.state == "CA"
    assert parse_location("Sample Insure (Series B) | ONSITE | New York City, NY").state == "NY"
    remote = parse_location("Example Labs | REMOTE (US Timezones) | Full-time")
    assert remote.is_us is True and remote.state is None and remote.remote
    assert parse_location("Demo Learning | Utrecht, The Netherlands | HYBRID").is_us is False
    assert parse_location("Thames Ledger | London, UK").is_us is False
    assert parse_location("Quiet Co | Engineer | Remote").is_us is None
    assert parse_location("").is_us is None


def test_parse_location_avoids_degree_false_positive():
    assert parse_location("Requires a BS/MS degree").is_us is None


def test_normalize_state():
    assert normalize_state("oregon") == "OR"
    assert normalize_state("tx") == "TX"
    assert normalize_state("Ontario") is None


def test_match_services_needs_specific_evidence():
    ids = [m.service_id for m in match_services("We need a Salesforce CRM migration and to automate manual processes")]
    assert "custom_crm" in ids and "bpa" in ids
    # generic stack words alone are not a requirement
    assert match_services("We use python and react") == []


def test_industry_and_competitor_detection():
    assert classify_industry("Mortgage and real estate fintech") == "real_estate"
    assert classify_industry("Shopify DTC brand") == "ecommerce"
    assert detect_competitor("We are a software development agency building for clients")
    assert not detect_competitor("We sell accounting software")


def test_dedupe_helpers(ctx):
    assert normalize_name("The Acme Corp, Inc.") == "acme"
    assert domain_from_url("https://www.Example.co.uk/path") == "example.co.uk"
    assert domain_from_url("acme.com") == "acme.com"
    assert domain_from_email("x@gmail.com") is None
    assert domain_from_email("x@sales.acme.com") == "acme.com"
    with ctx.db.session() as s:
        s.add(Company(name="Acme Corp", normalized_name="acme", domain="acme.com", state="TX", source="test"))
        s.flush()
        assert find_duplicate(s, "Acme", None, "TX") is not None
        assert find_duplicate(s, "Totally Different", "acme.com", None) is not None
        assert find_duplicate(s, "Acme", "other.com", "TX") is None  # same name, different site
        assert find_duplicate(s, "Acme", None, "CA") is None
