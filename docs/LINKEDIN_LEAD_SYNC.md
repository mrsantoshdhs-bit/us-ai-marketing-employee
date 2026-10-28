# LinkedIn leads (Lead Sync API)

People who choose to share their details with the company on LinkedIn become leads in the CRM. Then the AI employee answers them, follows up, books the meeting and hands over to a Sales Manager.

There are two ways to get these people:
- **Your Page's events:** people register through the event's registration form. This is free.
- **Lead Gen Form ads:** people submit the form attached to an ad. This is paid, and only if you decide to run ads.

**The integration never does any of this:**
- read profiles, connections or messages;
- search LinkedIn, or scrape or automate the LinkedIn website;
- post or send anything to LinkedIn.

LinkedIn's terms forbid scraping, bots and building prospect lists from its data. A lead form is the authorized way to get people who asked to hear from you.

## How a lead flows

1. **Collection.** Every 30 minutes, the inbound job asks LinkedIn for new lead form responses. It reads only from the Pages and ad accounts a Super Admin chose. LinkedIn's instant notifications need a public HTTPS address, which this local dashboard does not have, so it checks on a timer instead.
2. **Recording.** Each response is recorded once, as an inbound inquiry on channel `linkedin_form`, keyed by LinkedIn's lead id.
   - Each run overlaps the previous one by an hour, so a lead LinkedIn lists late is still found.
   - The first sync looks back 30 days.
   - LinkedIn test leads are skipped.
3. **What is kept from the form:**
   - name, email (the work email wins), phone, job title, company, city, state, country;
   - company size, industry, seniority and job function;
   - your own questions and their answers;
   - the consent boxes the person ticked.

   Profile links, education and demographics are dropped. If LinkedIn does not return the form's definition, the email address is still recognized, and the other answers are kept under their question numbers.
4. **Into the CRM.** The CRM agent applies the same checks as to every inquiry: spam, suppression list, US location. It then creates the company, a contact and a hot lead.
   - The contact's consent is `opt_in` if they ticked a consent box, otherwise `inquiry`. The consent source names the form or event, for example "LinkedIn event: AI agents for US service businesses".
   - The lead is qualified like any other.
5. **First email.** A qualified lead gets a first email:
   - "Thanks for registering: <event>" for an event;
   - "Thanks for your interest in <service>" for a form.

   The 3-email inquiry sequence follows. Every email waits for a person, or for an active campaign that covers it (step 6).
6. **Campaigns.** An "asked us" campaign rule sends these emails without a person. In Automation Control, create a rule campaign with **Addresses = "the address of a person who asked us to contact them"**.
   - It covers replies and follow-ups to people from inquiry forms, LinkedIn forms and event registrations. It never covers cold outreach.
   - The outreach rules ("published", "role_or_published") never answer people who wrote to us.
   - The usual gates still apply: `OUTBOUND_EMAIL_ENABLED`, `REQUIRE_HUMAN_APPROVAL=false`, Hybrid or Automatic mode, running state, daily and total limits, send window, opt-outs and the suppression list.
   - Tick "Also answer these prospects on its own" to let the AI employee offer meeting times and book them in Automatic mode, as in PR #54.
7. **Handover.** Interested prospects and booked meetings are handed to the **Sales Managers** chosen in Sales settings. With two or more, each new opportunity goes to the one with the fewest open opportunities, and it stays with them.

## Setting it up

You do steps 1 to 4 on LinkedIn; none of them change anything here.

1. **Company Page.** You need a real Company Page for the business, with you as its super admin.
2. **A separate developer app.** Create it at linkedin.com/developers, only for leads.
   - Keep it apart from the posting app: a rejected app cannot apply again, and your posting connection stays safe.
   - Its name must not contain "Linked" or "In".
   - Associate it with the Company Page, then verify it as the Page's super admin.
3. **Request "Lead Sync API".** In the app's Products tab:
   - use your **business email**; personal addresses such as Gmail are rejected;
   - give the company's legal name, registered address and website;
   - choose the use case "Lead Sync to our own CRM" and "Notification".
4. **Privacy policy.** Publish one on the company website. Lead forms link to it.
5. **Once approved,** connect the app here:
   1. Add the redirect URL `http://127.0.0.1:8765/integrations/linkedin-leads/callback` to the app's Auth settings.
   2. In `.env`, set `LINKEDIN_LEADS_CLIENT_ID` and `LINKEDIN_LEADS_CLIENT_SECRET`. You type them; they are never shown.
   3. Restart the dashboard.
   4. As a Super Admin, open **Integrations → LinkedIn leads** and click **Connect LinkedIn leads**.
6. **Choose what to sync.** Tick only your own company's Page, and its ad accounts if you run ads.
   - LinkedIn lists every Page where you hold a role that may read leads. A Page of another business you manage must stay unticked, or its leads would enter this CRM.
   - Ad accounts need `r_ads` added to `LINKEDIN_LEADS_SCOPES` before you connect.
7. **Test with LinkedIn test leads.** Run this in the acceptance environment:
   1. Set `LINKEDIN_LEADS_TEST_ONLY=true` and `LINKEDIN_LEADS_ENABLED=true`.
   2. In Campaign Manager, send a test lead from the ad preview (or register for your own test event).
   3. Run the inbound job.

   Test leads are stored as sample data, which the production database refuses.
8. **Go live.** In production, set `LINKEDIN_LEADS_ENABLED=true` and leave `LINKEDIN_LEADS_TEST_ONLY=false`.

**Before approval:** download the leads (Campaign Manager), or the registrant list (the event's attendee management), as CSV. Drop the file in the inbox folder (`data/inbox`).
- LinkedIn's column names are recognized: "First Name", "Company Name", "Job Title", "Country/Region", "Lead ID".
- A lead with a Lead ID is recorded once, even if it is in two files.

## Data protection

- **Connection.** The token is encrypted with Windows DPAPI.
  - A connection lasts 60 days. If LinkedIn grants this app renewal tokens, it is renewed a day before expiry. Otherwise the dashboard alerts you to connect again, and nothing is fetched meanwhile.
  - A refused connection (401) raises an alert and stops the sync until you reconnect.
- **Audit.** Connecting, disconnecting and choosing Pages are all in the audit log.
- **Deleting a person's data on request.** LinkedIn's API terms require it, and so do privacy laws. Run:

  ```
  python -m aime erase person@example.com --confirm
  ```

  This erases their contact details, inquiries, the emails they sent, our emails to them and the activity lines naming them. The address is kept only on the suppression list, so nothing is ever sent to it again. The audit log records the erasure without the address.

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `LINKEDIN_LEADS_ENABLED` | `false` | The switch: nothing is read from LinkedIn while it is off |
| `LINKEDIN_LEADS_CLIENT_ID` / `LINKEDIN_LEADS_CLIENT_SECRET` | empty | The separate app approved for the Lead Sync API |
| `LINKEDIN_LEADS_REDIRECT_URI` | `http://127.0.0.1:8765/integrations/linkedin-leads/callback` | Must match the app's Auth settings |
| `LINKEDIN_LEADS_SCOPES` | `r_marketing_leadgen_automation r_organization_admin` | Add `r_ads` to list ad accounts |
| `LINKEDIN_LEADS_TEST_ONLY` | `false` | Fetch only LinkedIn test leads, stored as sample data |

## API calls

All calls go to `https://api.linkedin.com/rest` with the `LinkedIn-Version` header (`LINKEDIN_API_VERSION`).

| Call | What it is for |
|---|---|
| `organizationAcls?q=roleAssignee` | Pages where the member may read leads |
| `organizations/{id}` | Page names |
| `adAccountUsers?q=authenticatedUser` | Ad accounts, with `r_ads` |
| `adAccounts/{id}` | Ad account names |
| `leadForms/{id}` | Field names of a form |
| `leadFormResponses?q=owner&owner=…&leadType=…&submittedAtTimeRange=…` | The leads: `EVENT`, `COMPANY` and `ORGANIZATION_PRODUCT` for a Page; `SPONSORED` for an ad account |

## Tests

`tests/test_linkedin_leads.py` runs every case against a fake LinkedIn server; CI also blocks real LinkedIn at the network level. It covers:
- **Switches:** nothing is read while the switch is off, before connecting, or before a Page is chosen.
- **Connecting:** the token is encrypted and the client secret is never shown; a tampered sign-in is refused; only Pages LinkedIn listed can be chosen.
- **Reading:** only chosen Pages are read, and test leads are skipped.
- **Recording:** each lead is recorded once across overlapping runs; consent and the form name are on the contact; the profile link is dropped.
- **Test-only mode:** it fetches only test leads, stored as sample data.
- **First email:** "Thanks for registering", waiting for approval.
- **Campaigns:** an "asked us" campaign sends it, and an outreach rule does not; an "asked us" rule never covers cold outreach or researched contacts.
- **Connection problems:** renewal before expiry; an alert when LinkedIn refuses; an expired connection is reported, not retried.
- **CSV downloads:** recognized and recorded once.
- **Handover:** two Sales Managers share new opportunities.
- **Erasure:** a person's data is erased, they stay suppressed, and the audit entry has no address.
- **Access:** only a Super Admin connects.
- **No form definition:** the address is still found and the profile link still dropped.
