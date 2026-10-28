"""LinkedIn Lead Sync (official API): the people who chose to share their details with you on LinkedIn - your Lead
Gen Form ads and the registration forms of events your Company Page organizes - come into the CRM as inbound
inquiries. Nothing else is read: no profiles, connections, messages or searches, and nothing is posted or sent.

It uses its own LinkedIn developer app, approved for the Lead Sync API (r_marketing_leadgen_automation), so the
posting connection (aime/integrations/linkedin.py) is never touched. Off until all of these hold:
LINKEDIN_LEADS_ENABLED=true, the app's client id and secret in .env, the account connected on the Integrations page,
and a Super Admin has chosen which LinkedIn Pages and ad accounts to sync (a member may manage Pages of other
businesses; their leads must never enter this CRM).

LinkedIn's lead notifications need a public HTTPS address, which this local dashboard does not have, so new leads
are fetched by the inbound job. Every lead is recorded once (by LinkedIn's lead id) and goes through the same
intake, spam checks, suppression list and qualification as any other inquiry. LinkedIn test leads are skipped unless
LINKEDIN_LEADS_TEST_ONLY=true, which fetches only test leads and stores them as sample data (never accepted by the
production database). Only the form's own answers are kept: name, email, phone, job title, company, location, a few
company facts and the answers to your own questions; profile links, education and demographics are dropped.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.db import kv_get, kv_set
from aime.integrations.linkedin import AUTH_URL, TOKEN_URL, LinkedInAPIError
from aime.models import OAuthToken
from aime.secure_store import protect, unprotect
from aime.timeutil import utcnow

API = "https://api.linkedin.com/rest"
TOKEN_KEY = "linkedin:leads"  # noqa: S105 (the token row's name, not a secret)
STATE_KEY = "linkedin_leads.oauth_state"
FOUND_KEY = "linkedin_leads.found"  # Pages and ad accounts the connected member may read leads from
OWNERS_KEY = "linkedin_leads.owners"  # the ones a Super Admin chose to sync
CURSOR_KEY = "linkedin_leads.cursor"  # {"<owner urn>|<lead type>": epoch ms fetched up to}
LAST_KEY = "linkedin_leads.last_sync"
NEEDED_SCOPE = "r_marketing_leadgen_automation"
STATE_TTL = timedelta(minutes=15)
FIRST_WINDOW = timedelta(days=30)  # the first sync looks back this far
OVERLAP = timedelta(hours=1)  # a lead LinkedIn lists late is still found; repeats are skipped by lead id
REFRESH_BEFORE = timedelta(days=1)
PAGE_SIZE, MAX_PAGES = 100, 20
PAGE_ROLES = {"ADMINISTRATOR", "LEAD_GEN_FORMS_MANAGER", "CURATOR", "CONTENT_ADMINISTRATOR", "ANALYST"}
ORG_LEAD_TYPES = ("EVENT", "COMPANY", "ORGANIZATION_PRODUCT")
TYPE_LABELS = {"SPONSORED": "LinkedIn ad form", "EVENT": "LinkedIn event", "COMPANY": "LinkedIn Page form",
               "ORGANIZATION_PRODUCT": "LinkedIn product page form"}
# Predefined form fields that are kept, and where they go; the order decides which wins (a work email over a
# personal one). Every other predefined field (profile link, education, demographics) is dropped.
FIELDS = [("FIRST_NAME", "first_name"), ("LAST_NAME", "last_name"), ("EMAIL", "email"), ("WORK_EMAIL", "email"),
          ("PHONE_NUMBER", "phone"), ("WORK_PHONE_NUMBER", "phone"), ("JOB_TITLE", "title"),
          ("COMPANY_NAME", "company"), ("CITY", "city"), ("STATE", "state"), ("COUNTRY", "country")]
CONTEXT = {"COMPANY_SIZE": "Company size", "INDUSTRY": "Industry", "SENIORITY": "Seniority",
           "JOB_FUNCTION": "Job function"}
FORM_ID_RE = re.compile(r"leadGenForm:(\d+)")
EMAIL_LIKE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class LeadSyncError(RuntimeError):
    """A connection or sign-in problem, in words for the owner (never contains a token)."""


@dataclass
class LeadSyncStatus:
    configured: bool
    enabled: bool
    connected: bool
    ready: bool
    reason: str
    scopes: str = ""
    expires_at: datetime | None = None
    owners: list[dict] = field(default_factory=list)
    found: list[dict] = field(default_factory=list)
    last_sync: dict | None = None
    test_only: bool = False


@dataclass
class SyncResult:
    ran: bool = False
    reason: str = ""
    fetched: int = 0
    recorded: int = 0
    already: int = 0
    test_skipped: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def get_token(session: Session) -> OAuthToken | None:
    return session.execute(select(OAuthToken).where(OAuthToken.provider == TOKEN_KEY)).scalars().first()


def status(settings: Settings, session: Session, now: datetime | None = None) -> LeadSyncStatus:
    now = now or utcnow()
    tok = get_token(session)
    common = dict(owners=kv_get(session, OWNERS_KEY) or [], found=kv_get(session, FOUND_KEY) or [],
                  last_sync=kv_get(session, LAST_KEY), test_only=settings.linkedin_leads_test_only,
                  scopes=tok.scope if tok else "", expires_at=tok.expires_at if tok else None)
    enabled, configured = settings.linkedin_leads_enabled, settings.linkedin_leads_configured
    if not configured:
        return LeadSyncStatus(False, enabled, False, False, "Not configured: set LINKEDIN_LEADS_CLIENT_ID and "
                              "LINKEDIN_LEADS_CLIENT_SECRET (a separate app approved for the Lead Sync API)", **common)
    if tok is None:
        return LeadSyncStatus(True, enabled, False, False, "Not connected: click Connect LinkedIn leads", **common)
    if NEEDED_SCOPE not in tok.scope.replace(",", " ").split():
        return LeadSyncStatus(True, enabled, True, False, f"The connection lacks {NEEDED_SCOPE}: is the app approved "
                              "for the Lead Sync API? Connect again once it is", **common)
    if tok.expires_at and tok.expires_at <= now and not tok.refresh_token:
        return LeadSyncStatus(True, enabled, True, False, "The connection expired: connect LinkedIn leads again", **common)
    if not common["owners"]:
        return LeadSyncStatus(True, enabled, True, False, "Connected: choose which LinkedIn Pages and ad accounts "
                              "to sync", **common)
    if not enabled:
        return LeadSyncStatus(True, False, True, False, "Ready, but LINKEDIN_LEADS_ENABLED=false (the switch is off)",
                              **common)
    names = ", ".join(o.get("name") or o["urn"] for o in common["owners"])
    return LeadSyncStatus(True, True, True, True, f"Syncing leads from {names}"
                          + (" (LinkedIn test leads only)" if settings.linkedin_leads_test_only else ""), **common)


class LeadSyncClient:
    def __init__(self, settings: Settings, http: httpx.Client | None):
        self.settings = settings
        self.http = http

    def authorization_url(self, state: str) -> str:
        return f"{AUTH_URL}?" + urlencode({
            "response_type": "code", "client_id": self.settings.linkedin_leads_client_id,
            "redirect_uri": self.settings.linkedin_leads_redirect_uri, "state": state,
            "scope": self.settings.linkedin_leads_scopes})

    def _token(self, data: dict) -> dict:
        resp = self.http.post(TOKEN_URL, data={**data, "client_id": self.settings.linkedin_leads_client_id,
                                               "client_secret": self.settings.linkedin_leads_client_secret},
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            raise LinkedInAPIError(resp.status_code, resp.text)
        return resp.json()

    def exchange_code(self, code: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code,
                            "redirect_uri": self.settings.linkedin_leads_redirect_uri})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _get(self, token: str, url: str) -> dict:
        resp = self.http.get(url, headers={"Authorization": f"Bearer {token}",
                                           "LinkedIn-Version": self.settings.linkedin_api_version,
                                           "X-Restli-Protocol-Version": "2.0.0"})
        if resp.status_code != 200:
            raise LinkedInAPIError(resp.status_code, resp.text)
        return resp.json()

    def pages(self, token: str) -> list[dict]:
        """LinkedIn Pages where the member holds a role that may read lead forms."""
        data = self._get(token, f"{API}/organizationAcls?q=roleAssignee&state=APPROVED&count=100")
        found = {}
        for e in data.get("elements", []):
            urn = e.get("organization") or ""
            if urn.startswith("urn:li:organization:") and e.get("role") in PAGE_ROLES:
                found.setdefault(urn, {"urn": urn, "kind": "page", "role": e["role"], "name": None})
        for item in found.values():
            try:
                item["name"] = self._get(token, f"{API}/organizations/{item['urn'].rsplit(':', 1)[1]}").get("localizedName")
            except (LinkedInAPIError, httpx.HTTPError):
                pass
        return list(found.values())

    def ad_accounts(self, token: str) -> list[dict]:
        """Ad accounts the member works in (needs r_ads; without it, none)."""
        try:
            data = self._get(token, f"{API}/adAccountUsers?q=authenticatedUser")
        except LinkedInAPIError as exc:
            if exc.status in (401, 403):
                return []
            raise
        found = []
        for e in data.get("elements", []):
            urn = e.get("account") or ""
            if urn.startswith("urn:li:sponsoredAccount:"):
                item = {"urn": urn, "kind": "ad_account", "role": e.get("role"), "name": None}
                try:
                    item["name"] = self._get(token, f"{API}/adAccounts/{urn.rsplit(':', 1)[1]}").get("name")
                except (LinkedInAPIError, httpx.HTTPError):
                    pass
                found.append(item)
        return found

    def form(self, token: str, form_id: str) -> dict:
        return self._get(token, f"{API}/leadForms/{form_id}")

    def responses(self, token: str, owner: str, lead_type: str, start_ms: int, end_ms: int,
                  test_only: bool) -> list[dict]:
        kind = "organization" if owner.startswith("urn:li:organization:") else "sponsoredAccount"
        out: list[dict] = []
        for page in range(MAX_PAGES):
            url = (f"{API}/leadFormResponses?q=owner&owner=({kind}:{quote(owner, safe='')})"
                   f"&leadType=(leadType:{lead_type})&limitedToTestLeads={'true' if test_only else 'false'}"
                   f"&submittedAtTimeRange=(start:{start_ms},end:{end_ms})&start={page * PAGE_SIZE}&count={PAGE_SIZE}")
            elements = self._get(token, url).get("elements", [])
            out.extend(elements)
            if len(elements) < PAGE_SIZE:
                break
        return out


def store_token(session: Session, settings: Settings, data: dict) -> OAuthToken:
    tok = get_token(session) or OAuthToken(provider=TOKEN_KEY, access_token="")
    tok.access_token = protect(data["access_token"])  # encrypted at rest, never shown
    seconds = int(data.get("expires_in") or 0)
    tok.expires_at = utcnow() + timedelta(seconds=seconds) if seconds else None
    if data.get("refresh_token"):
        tok.refresh_token = protect(data["refresh_token"])
    tok.scope = (data.get("scope") or settings.linkedin_leads_scopes).replace(",", " ")[:300]
    session.add(tok)
    session.flush()
    return tok


def access_token(session: Session, settings: Settings, client: LeadSyncClient, now: datetime | None = None) -> str:
    now = now or utcnow()
    tok = get_token(session)
    if tok is None:
        raise LeadSyncError("LinkedIn leads is not connected")
    if tok.expires_at and tok.expires_at - now < REFRESH_BEFORE:
        if tok.refresh_token:
            try:
                tok = store_token(session, settings, client.refresh(unprotect(tok.refresh_token)))
            except LinkedInAPIError as exc:
                raise LeadSyncError(f"LinkedIn did not renew the leads connection ({exc.status}): connect it again "
                                    "on the Integrations page") from None
        elif tok.expires_at <= now:
            raise LeadSyncError("The LinkedIn leads connection expired: connect it again on the Integrations page")
    return unprotect(tok.access_token)


def start(session: Session, settings: Settings) -> str:
    if not settings.linkedin_leads_configured:
        raise LeadSyncError("Set LINKEDIN_LEADS_CLIENT_ID and LINKEDIN_LEADS_CLIENT_SECRET in .env first")
    state = secrets.token_urlsafe(24)
    kv_set(session, STATE_KEY, {"state": state, "at": utcnow().isoformat()})
    return LeadSyncClient(settings, None).authorization_url(state)


def finish(session: Session, settings: Settings, http: httpx.Client, state: str, code: str, actor: str) -> OAuthToken:
    from aime.automation import audit

    saved = kv_get(session, STATE_KEY) or {}
    kv_set(session, STATE_KEY, None)  # one use only
    fresh = saved.get("at") and utcnow() - datetime.fromisoformat(saved["at"]) < STATE_TTL
    if not state or not code or state != saved.get("state") or not fresh:
        raise LeadSyncError("The sign-in expired or did not match: please try again")
    client = LeadSyncClient(settings, http)
    try:
        tok = store_token(session, settings, client.exchange_code(code))
    except LinkedInAPIError as exc:
        raise LeadSyncError(f"LinkedIn did not accept the sign-in ({exc.status})") from None
    audit(session, actor, "account_connected", TOKEN_KEY, f"permissions: {tok.scope}")
    try:
        discover(session, client, unprotect(tok.access_token))
    except (LinkedInAPIError, httpx.HTTPError):
        pass  # the list can be refreshed from the Integrations page
    return tok


def discover(session: Session, client: LeadSyncClient, token: str) -> list[dict]:
    found = client.pages(token) + client.ad_accounts(token)
    kv_set(session, FOUND_KEY, found)
    return found


def refresh_found(session: Session, settings: Settings, http: httpx.Client) -> list[dict]:
    client = LeadSyncClient(settings, http)
    try:
        return discover(session, client, access_token(session, settings, client))
    except LinkedInAPIError as exc:
        raise LeadSyncError(f"LinkedIn did not list your Pages ({exc.status})") from None


def choose_owners(session: Session, urns: list[str], actor: str) -> list[dict]:
    """Keep only Pages and ad accounts LinkedIn listed for this member; audit the choice."""
    from aime.automation import audit

    found = {f["urn"]: f for f in kv_get(session, FOUND_KEY) or []}
    chosen = [found[u] for u in dict.fromkeys(urns) if u in found]
    kv_set(session, OWNERS_KEY, chosen)
    audit(session, actor, "linkedin_leads_owners", "LinkedIn leads",
          "sync from: " + (", ".join(c.get("name") or c["urn"] for c in chosen) or "nothing"))
    return chosen


def disconnect(session: Session, actor: str) -> bool:
    from aime.automation import audit

    tok = get_token(session)
    if tok is None:
        return False
    session.delete(tok)
    audit(session, actor, "account_disconnected", TOKEN_KEY, "tokens deleted")
    return True


def _text(value) -> str:
    if isinstance(value, dict):
        localized = value.get("localized") or {}
        return str(localized.get("en_US") or next(iter(localized.values()), "")).strip()
    return str(value or "").strip()


def payload_from(lead: dict, form: dict | None, test_only: bool = False) -> dict:
    """An inbound-inquiry payload from one lead form response (aime/inbound.py InboundPayload)."""
    content = (form or {}).get("content") or {}
    questions = {q.get("questionId"): q for q in content.get("questions", [])}
    by_field: dict[str, str] = {}
    extra: list[str] = []
    for ans in (lead.get("formResponse") or {}).get("answers", []):
        q = questions.get(ans.get("questionId"), {})
        details = ans.get("answerDetails") or {}
        if "textQuestionAnswer" in details:
            text = str(details["textQuestionAnswer"].get("answer") or "").strip()
        elif "multipleChoiceAnswer" in details:
            opts = (((q.get("questionDetails") or {}).get("multipleChoiceQuestionDetails") or {}).get("options") or [])
            names = {o.get("id"): _text(o.get("text")) for o in opts}
            text = ", ".join(names.get(i) or str(i) for i in details["multipleChoiceAnswer"].get("options", []))
        else:
            continue
        if not text or "linkedin.com/in/" in text.lower():  # a profile link is never kept
            continue
        predefined = q.get("predefinedField")
        if predefined in dict(FIELDS) or predefined in CONTEXT:
            by_field[predefined] = text[:300]
        elif not q and EMAIL_LIKE.match(text) and "EMAIL" not in by_field:
            by_field["EMAIL"] = text  # without the form's definition, the address is still recognized
        elif predefined is None:  # a question the form's owner wrote (or, without the form, by its number)
            question = _text(q.get("question")) or f"Answer {ans.get('questionId')}"
            extra.append(f"{question}: {text}"[:500])
    values: dict[str, str] = {}
    for predefined, key in FIELDS:
        if predefined in by_field:
            values[key] = by_field[predefined]
    extra = [f"{label}: {by_field[f]}" for f, label in CONTEXT.items() if f in by_field] + extra
    consents = {c.get("id"): _text(c.get("consent")) for c in (content.get("legalInfo") or {}).get("consents", [])}
    agreed = [consents.get(c.get("consentId")) or f"consent {c.get('consentId')}"
              for c in (lead.get("formResponse") or {}).get("consentResponses", []) if c.get("accepted")]
    label = TYPE_LABELS.get(lead.get("leadType") or "", "LinkedIn form")
    where = f"{label}: {(form or {}).get('name') or 'a lead form'}"[:200]
    lines = [where, *extra[:12]] + (["Agreed: " + "; ".join(agreed)] if agreed else [])
    return {**values, "message": "\n".join(lines)[:4000], "source_page": where, "consent_marketing": bool(agreed),
            "external_id": f"linkedin:{lead.get('id')}", "is_sample": test_only}


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def sync(session: Session, settings: Settings, http: httpx.Client, now: datetime | None = None) -> SyncResult:
    """Fetch new lead form responses from the chosen Pages and ad accounts into inbound inquiries (channel
    "linkedin_form"). The CRM agent turns them into companies, consented contacts and leads in the same run."""
    from aime.inbound import record_inquiry

    now = now or utcnow()
    st = status(settings, session, now)
    res = SyncResult(reason=st.reason)
    if not st.ready:
        return res
    client = LeadSyncClient(settings, http)
    token = access_token(session, settings, client, now)
    cursors = dict(kv_get(session, CURSOR_KEY) or {})
    forms: dict[str, dict | None] = {}
    end = _ms(now)
    for owner in st.owners:
        urn = owner["urn"]
        for lead_type in (("SPONSORED",) if urn.startswith("urn:li:sponsoredAccount:") else ORG_LEAD_TYPES):
            key = f"{urn}|{lead_type}"
            begin = cursors[key] - int(OVERLAP.total_seconds() * 1000) if key in cursors else _ms(now - FIRST_WINDOW)
            try:
                leads = client.responses(token, urn, lead_type, begin, end, settings.linkedin_leads_test_only)
            except LinkedInAPIError as exc:
                if exc.status == 401:
                    raise LeadSyncError("LinkedIn refused the leads connection (401): connect it again on the "
                                        "Integrations page") from None
                res.errors.append(f"{owner.get('name') or urn}, {TYPE_LABELS[lead_type]}: LinkedIn {exc.status}")
                continue
            for lead in sorted(leads, key=lambda x: x.get("submittedAt") or 0):
                res.fetched += 1
                if lead.get("testLead") and not settings.linkedin_leads_test_only:
                    res.test_skipped += 1
                    continue
                m = FORM_ID_RE.search(lead.get("versionedLeadGenFormUrn") or "")
                form_id = m.group(1) if m else None
                if form_id and form_id not in forms:
                    try:
                        forms[form_id] = client.form(token, form_id)
                    except LinkedInAPIError:
                        forms[form_id] = None  # the answers still arrive, labelled by question number
                try:
                    _row, created = record_inquiry(session, payload_from(lead, forms.get(form_id),
                                                                         settings.linkedin_leads_test_only),
                                                   "linkedin_form")
                except ValidationError:
                    res.rejected += 1
                    continue
                res.recorded += int(created)
                res.already += int(not created)
            cursors[key] = end
    kv_set(session, CURSOR_KEY, cursors)
    kv_set(session, LAST_KEY, {"at": now.isoformat(), "fetched": res.fetched, "recorded": res.recorded,
                               "test_skipped": res.test_skipped, "errors": res.errors[:5]})
    res.ran = True
    return res
