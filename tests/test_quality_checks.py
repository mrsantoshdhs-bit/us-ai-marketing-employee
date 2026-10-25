"""Automated content quality checks: what fails, what is held for a person, and how the agent and dashboard react."""

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from aime.agents import LinkedInContentAgent
from aime.models import Activity, ContentItem
from aime.qc import decide, CheckResult
from aime.qc.checks import (check_brand, check_claims, check_cliches, check_grammar, check_linkedin_rules,
                            check_spelling, image_hash, run_quality_checks)
from aime.timeutil import utcnow
from aime.web.app import create_app

GOOD_BODY = """Every manual handoff between two systems is a place where work waits and mistakes creep in.

For real estate teams, it often looks like transaction paperwork re-keyed into several systems. None of it is anyone's main job, which is exactly why it never gets fixed.

Where to start:
• List the tasks people repeat every day or week
• Find where data is re-typed from one tool into another
• Automate the handoff, and keep a person on the exceptions

Automation works best when it removes the boring steps and leaves the judgment calls to people.

At Digital Hub Solution, we build automation that removes copy-paste work between the tools your team already uses.

Where does work get stuck in your team's week?"""
TAGS = ["#BusinessAutomation", "#ProcessImprovement", "#RealEstate"]
ORIGIN = {"origin": "http://testserver"}


def _image(path: Path, color=(20, 40, 80), size=(1080, 1350)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def test_decision_rules():
    assert decide([CheckResult("a", "pass"), CheckResult("b", "pass")]) == "pass"
    assert decide([CheckResult("a", "pass"), CheckResult("b", "uncertain")]) == "hold"
    assert decide([CheckResult("a", "uncertain"), CheckResult("b", "fail")]) == "fail"


def test_spelling_catches_typos_but_not_business_words_or_contractions():
    assert check_spelling("Title", "We recieve and seperate the data.").status == "fail"
    ok = check_spelling("SaaS onboarding", "Our CRM workflow doesn't need a chatbot; admin work is the team's problem.")
    assert ok.status == "pass", ok.detail


def test_grammar_rules():
    assert check_grammar("T", "This is is wrong.").status == "fail"
    assert check_grammar("T", "It works. then it stops.").status == "fail"
    assert check_grammar("T", "Use a agent wisely.").status == "fail"
    assert check_grammar("T", "A user needs an agent (and a plan).").status == "pass"


def test_claims_with_numbers_or_named_companies_are_held_for_a_person():
    assert check_claims("T", "Teams save 40% with this.").status == "uncertain"
    assert "Salesforce" in check_claims("T", "Salesforce agrees with us.").detail
    assert check_claims("T", "Plain practical advice only.").status == "pass"


def test_brand_and_hashtag_rules(ctx):
    assert check_brand("T", "At Digital Hub Solutions we build", TAGS, ctx.settings).status == "fail"
    assert check_brand("T", "At Digital Hub Solution we build", TAGS + ["#Crypto"], ctx.settings).status == "fail"
    assert check_brand("T", "At Digital Hub Solution we build", TAGS, ctx.settings).status == "pass"


def test_linkedin_rules_and_cliches():
    assert check_linkedin_rules("T", "Comment YES below!", []).status == "fail"
    assert check_linkedin_rules("T", "Thanks @someone for this.", []).status == "fail"
    assert check_linkedin_rules("T", "Read more at https://example.com/page", []).status == "uncertain"
    assert check_cliches("T", "In today's fast-paced market we leverage AI.").status == "fail"


def test_full_report_passes_good_post_and_detects_duplicates(ctx, tmp_path):
    img = _image(tmp_path / "a.png")
    with ctx.db.session() as s:
        rep = run_quality_checks(s, ctx.settings, title="The hidden cost of copy-paste work", body=GOOD_BODY,
                                 hashtags=TAGS, image_path=img, image_info={"renderer": "pillow"})
        assert rep["decision"] == "pass", rep
        s.add(ContentItem(service="bpa", industry="real_estate", title="The hidden cost of copy-paste work",
                          body=GOOD_BODY, hashtags=TAGS, generator="t", fingerprint="f", status="awaiting_approval",
                          quality={"image": {"phash": rep["image_phash"]}}))
        s.flush()
        dup = run_quality_checks(s, ctx.settings, title="A different title", body=GOOD_BODY, hashtags=TAGS,
                                 image_path=_image(tmp_path / "b.png"), image_info={"renderer": "pillow"})
    detail = next(c["detail"] for c in dup["checks"] if c["name"] == "duplicates")
    assert dup["decision"] == "fail" and "similar" in detail and "image nearly identical" in detail


def test_image_hash_distinguishes_different_images(tmp_path):
    a = image_hash(_image(tmp_path / "a.png"))
    img = Image.new("RGB", (1080, 1350), (250, 250, 250))
    for x in range(0, 1080, 120):
        for y in range(0, 1350, 150):
            img.paste((10, 10, 10), (x, y, x + 60, y + 75))
    img.save(tmp_path / "b.png")
    b = image_hash(tmp_path / "b.png")
    assert bin(int(a, 16) ^ int(b, 16)).count("1") > 6


def test_agent_replaces_failing_drafts_and_stores_the_report(ctx, monkeypatch):
    calls = {"n": 0}
    real = run_quality_checks

    def first_draft_fails(*a, **k):
        calls["n"] += 1
        rep = real(*a, **k)
        if calls["n"] == 1:
            rep.update(decision="fail", status="failed")
            rep["checks"].append({"name": "spelling", "status": "fail", "detail": "simulated typo"})
        return rep
    monkeypatch.setattr("aime.agents.linkedin_content.run_quality_checks", first_draft_fails)
    out = LinkedInContentAgent(ctx).run({"research_topics": False}).output
    assert out.created and len(out.qc_rejected) == 1 and "simulated typo" in out.qc_rejected[0]
    with ctx.db.session() as s:
        items = s.execute(select(ContentItem)).scalars().all()
        assert all(i.qc_status in ("passed", "held") for i in items)
        assert all(i.qc["content_sha256"] for i in items)


def test_held_post_needs_explicit_review_before_approval(ctx):
    with ctx.db.session() as s:
        item = ContentItem(service="bpa", industry="real_estate", title="T", body="Body?", hashtags=[], generator="t",
                           fingerprint="f", status="awaiting_approval", scheduled_for=utcnow() + timedelta(days=2),
                           quality={"qc": {"status": "held", "checks": [
                               {"name": "factual claims", "status": "uncertain", "detail": "contains a number"}]}})
        s.add(item)
        s.flush()
        cid = item.id
    client = TestClient(create_app(ctx))
    r = client.post(f"/content/{cid}/approve", headers=ORIGIN, follow_redirects=False)
    assert "reviewed" in r.headers["location"]
    with ctx.db.session() as s:
        assert s.get(ContentItem, cid).status == "awaiting_approval"
    client.post(f"/content/{cid}/approve", data={"qc_override": "yes"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(ContentItem, cid).status == "approved"
        assert s.execute(select(Activity).where(Activity.subject.like("%despite quality findings%"))).first()


def test_saving_an_edit_reruns_the_checks(ctx):
    with ctx.db.session() as s:
        item = ContentItem(service="bpa", industry="real_estate", title="The hidden cost of copy-paste work",
                           body=GOOD_BODY, hashtags=TAGS, generator="t", fingerprint="f", status="awaiting_approval")
        s.add(item)
        s.flush()
        cid = item.id
    client = TestClient(create_app(ctx))
    client.post(f"/content/{cid}/save", data={"body": GOOD_BODY.replace("handoff", "handof")}, headers=ORIGIN)
    with ctx.db.session() as s:
        item = s.get(ContentItem, cid)
        assert item.qc_status == "failed" and any(c["name"] == "spelling" and c["status"] == "fail"
                                                  for c in item.qc["checks"])
