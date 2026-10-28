# Lead data audit, 28 October 2026

This audit separates genuine, evidence-backed US prospects from demo, test and unknown records, for every company, contact and lead.

- **Production database:** read-only (`mode=ro`), at about 11:30 IST, while production ran `2288f38` with sending, mailbox reading and publishing off. Nothing was written.
- **Aggregates only:** this repository is **public**, so this file contains only counts. Company names, contact names and email addresses do not appear here.
- **Record-level results** are kept on the owner's PC, outside the repository, in `Documents\aime-acceptance\AUDIT-2026-10-28\` (`companies_`, `contacts_` and `leads_classification.csv`).

## Classification rules
| Category | Rule (all must hold) |
|---|---|
| Genuine US prospect, website verified | Not sample/test; not merged; has a source URL and at least one signal with a source URL; explicit US-location evidence (a US state, or location text naming the US); not a flagged competitor; `website_status = verified` |
| Genuine US prospect, website not verified | Same as above, but the website check is pending, blocked, unreachable, redirected or mismatched |
| Flagged competitor | As above, but `is_competitor` is set and no person has cleared it; outreach is blocked |
| Demo/test/sample | `is_sample`, a test domain (`example.`, `.test`, `.invalid`, `localhost`), or the owner's own company |
| Unknown provenance or location | No source URL, no evidenced signal, or no US-location evidence |

`country = 'US'` is the column's default, so on its own it is **not** treated as evidence. The location evidence is in `state` and `location_text`. Discovery also refuses candidates whose US location is unknown or non-US (`aime/agents/research.py:97-102`).

## Companies (280)
| Category | Count |
|---|---|
| **Genuine US prospect, website verified** | **131** |
| **Genuine US prospect, website not verified** | **139**: 113 not checked yet, 12 blocked, 6 mismatch, 6 unreachable, 2 redirected |
| Flagged competitor (excluded from outreach; none cleared by a person) | 10 |
| Demo/test/sample | **0** |
| Unknown provenance or location | **0** |
| Merged duplicates | 0 |

- **Sources:**
  - Hacker News "Who is hiring" posts: 235, each linked to its post.
  - SEC EDGAR filings: 45, each linked to its filing, with the registrant's business-address state.
  - These are the only two sources in production.
- **Evidence:** 324 signals (279 hiring posts, 45 filings), every one with a source URL.
- **Evidence age:** hiring posts date from 1 September to 14 October 2026; SEC filings were read 26–28 October.
- **US-location evidence:**
  - 207 companies by US state;
  - 73 by location text: 72 say "remote" and name the US, 1 names a US city.
- **Research depth:**
  - 45 companies have had their own website and/or SEC filings read (60 research snapshots), at about 15 a day (the per-run limit).
  - Websites are checked at about 20 a day, after a first batch of 114.

## Contacts (129)
| Category | Count |
|---|---|
| Evidence-backed named person (from the company's own website, with the page URL), **no published email** | 119 |
| Evidence-backed **company role mailbox** (info@ / hello@ / contact@) published on the company's own site, mail server checked | 7 |
| Not usable: no longer on the company's website (`website_withdrawn`) | 3 |
| Evidence-backed **personal** email published by the company | **0** |
| Demo/test/sample, user-provided, opted out | 0 |

Roles of the named people: executive 59, technology 12, operations 10, sales 9, board 5, finance 4, marketing 4, people 3, other 16, plus 7 role mailboxes.

## Leads (279, all from research)
| Company category | Qualified | Nurture | Disqualified |
|---|---|---|---|
| Genuine, website verified | 33 | 98 | 0 |
| Genuine, website not verified | 9 | 130 | 0 |
| Flagged competitor | 0 | 0 | 9 |

- **Grades:** B 51, C 139, D 89. There are **no grade-A leads**.
- **Outreach readiness:** **42** qualified genuine leads, of which **22** have a named decision-maker and **5** have a usable email address. All 5 addresses are company role mailboxes.
- **Research depth:** 34 of the 42 qualified leads have been researched in depth.

## Outreach, replies, meetings
- **Outreach drafts:** 6, all to company role mailboxes.
  - 5 approved: #38 and #46 on 27 Oct; #51, #52 and #53 by `admin` on 28 Oct, 11:07–11:08 IST.
  - 1 cancelled, because the lead was flagged as a competitor.
- **Sent:** none. Sending is off (`OUTBOUND_EMAIL_ENABLED=false`), so all five approved drafts are held. They were approved while sending was off, so each will need "Confirm again" before it can go out.
- **Pipeline:** replies 0, meetings 0, opportunities 0, suppressions 0, inbound inquiries 0.

## Non-production data, kept apart from the prospects above
| Dataset | What it is | Notes |
|---|---|---|
| `Documents\aime-acceptance\data` | A copy of production taken on 26 Oct, used for acceptance tests with the owner's real Gmail | 242 companies, 79 contacts. Includes 2 user-provided test contacts (the owner's own company), 2 test-mode emails delivered to the owner's own Gmail, and 4 real messages from the owner's mailbox tests. **It is labelled `production`** (`acceptance.env` sets no `AIME_DATA_ROLE`), so it shows no acceptance banner. |
| `demo_data\aime.db` | Demo database, role `demo` | 6 companies (3 flagged sample), on an older schema |
| `Documents\aime-acceptance\PREVIEW-*`, `DEPLOYCHECK-*`, `DRYRUN-*`, `ROLLBACKCHECK-*`, `SIMULATION-*`, `PAGESWEEP-*` | Throw-away copies for previews, deployment rehearsals and simulations | LinkedIn token removed in the preview copies; never used by the scheduler |
| Test suite | Fictional companies in temporary folders | Never touches `data\` |

**Production contains 0 sample records, 0 test domains and 0 user-provided contacts.** The doctor's "No sample data in production" check passes.

## Findings
1. **Email reachability is the bottleneck.** Only 5 of 42 qualified leads can be emailed, all through generic role mailboxes. The free sources in use (company websites, SEC filings) rarely publish a decision-maker's own address, and paid enrichment was excluded.
2. **Website-check backlog.** 113 companies have never had their website checked. At about 20 a day this takes 5–6 days, and unchecked companies are not researched in depth.
3. **Research depth.** 45 of 280 companies (16%) have had their own website read.
4. **No grade-A leads.** Scores are capped mostly by a missing reachable contact and thin intent data.
5. **Ageing evidence.** Hiring-post evidence is up to 8 weeks old and nothing marks it stale.
6. **Location is implicit.** It is inferred from `state` or `location_text`; there is no stored "US evidence" field, and `country` defaults to US.
7. **Acceptance label.** The acceptance database carries the `production` role label, which makes it easy to confuse with production.
