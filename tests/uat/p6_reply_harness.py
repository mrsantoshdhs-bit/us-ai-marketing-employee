"""Real InboxAgent + fake IMAP (RFC822 bytes). Synthetic data only."""
from __future__ import annotations

from email.message import EmailMessage
from html import escape

from sqlalchemy import select

from aime.crm_ops import is_suppressed
from aime.models import Alert, FollowUp, InboundEmail, Lead, Opportunity
from aime.timeutil import utcnow
from tests.test_inbox import DAVID, FakeIMAP, contacted, run_inbox

SALES = "Sales <sales@us.example>"
_n = [0]


def q(sent) -> str:
    return "\n".join("> " + x for x in sent.body.splitlines())


def block(sent) -> str:
    return sent.body


GMAIL = lambda s: f"\n\nOn Tue, Oct 27, 2026 at 11:02 AM {SALES} wrote:\n\n{q(s)}\n"
GMAIL_WRAP = lambda s: f"\n\nOn Tue, Oct 27, 2026 at 11:02 AM Sales <\nsales@us.example> wrote:\n\n{q(s)}\n"
IPHONE = lambda s: f"\n\nSent from my iPhone\n\n> On Oct 27, 2026, at 11:02 AM, {SALES} wrote:\n> \n{q(s)}\n"
OUTLOOK = lambda s: f"\n\n________________________________\nFrom: {SALES}\nSent: Tuesday, October 27, 2026 11:02 AM\nTo: David Chen <{DAVID}>\nSubject: {s.subject}\n\n{block(s)}\n"
OUTLOOK_BARE = lambda s: f"\n\nFrom: {SALES}\nSent: Tuesday, October 27, 2026 11:02 AM\nTo: David Chen <{DAVID}>\nSubject: {s.subject}\n\n{block(s)}\n"
OUTLOOK_STAR = lambda s: f"\n\nFrom: Sales [mailto:sales@us.example]\nSent: Tuesday, October 27, 2026 11:02 AM\nTo: David Chen\nSubject: {s.subject}\n\n{block(s)}\n"
ORIG = lambda s: f"\n\n-----Original Message-----\nFrom: {SALES}\nSent: Tuesday, October 27, 2026 11:02 AM\nTo: David Chen\nSubject: {s.subject}\n\n{block(s)}\n"
OUTLOOK_MAC = lambda s: f"\n\nFrom: {SALES}\nDate: Tuesday, October 27, 2026 at 11:02 AM\nTo: David Chen <{DAVID}>\nSubject: {s.subject}\n\n{block(s)}\n"
OUTLOOK_IOS = lambda s: f"\n\nGet Outlook for iOS\n________________________________\nFrom: {SALES}\nSent: Tuesday, October 27, 2026 11:02:13 AM\nTo: David Chen <{DAVID}>\nSubject: {s.subject}\n\n{block(s)}\n"
SAMSUNG = lambda s: f"\n\nSent from my Galaxy\n\n\n-------- Original message --------\nFrom: {SALES}\nDate: 10/27/26 11:02 AM (GMT-05:00)\nTo: {DAVID}\nSubject: {s.subject}\n\n{block(s)}\n"
YAHOO = lambda s: f"\n\nSent from Yahoo Mail on Android\n\n  On Tue, Oct 27, 2026 at 11:02 AM, Sales<sales@us.example> wrote:\n  {block(s)}\n"
THUNDERBIRD = lambda s: f"\n\nOn 10/27/26 11:02 AM, Sales wrote:\n{q(s)}\n"
PROTON = lambda s: f"\n\n------- Original Message -------\nOn Tuesday, October 27th, 2026 at 11:02 AM, Sales <sales@us.example> wrote:\n\n{q(s)}\n"
ZIMBRA = lambda s: f"\n\n----- Original Message -----\nFrom: \"Sales\" <sales@us.example>\nTo: \"David Chen\" <{DAVID}>\nSent: Tuesday, October 27, 2026 11:02:00 AM\nSubject: {s.subject}\n\n{block(s)}\n"
BARE_WROTE = lambda s: f"\n\nSales wrote:\n{block(s)}\n"
FWD = lambda s: f"\n\n---------- Forwarded message ---------\nFrom: {SALES}\nDate: Tue, Oct 27, 2026 at 11:02 AM\nSubject: {s.subject}\nTo: David Chen <{DAVID}>\n\n{block(s)}\n"
BEGIN_FWD = lambda s: f"\n\nBegin forwarded message:\n\nFrom: {SALES}\nSubject: {s.subject}\nDate: October 27, 2026 at 11:02:00 AM EDT\nTo: David Chen <{DAVID}>\n\n{block(s)}\n"
ES_GMAIL = lambda s: f"\n\nEl mar, 27 oct 2026 a las 11:02, Sales (<sales@us.example>) escribió:\n\n{q(s)}\n"
FR_GMAIL = lambda s: f"\n\nLe mar. 27 oct. 2026 à 11:02, Sales <sales@us.example> a écrit\xa0:\n\n{q(s)}\n"
FR_OUTLOOK = lambda s: f"\n\n________________________________\nDe : Sales <sales@us.example>\nEnvoyé : mardi 27 octobre 2026 11:02\nÀ : David Chen <{DAVID}>\nObjet : {s.subject}\n\n{block(s)}\n"
ES_OUTLOOK = lambda s: f"\n\n________________________________\nDe: Sales <sales@us.example>\nEnviado: martes, 27 de octubre de 2026 11:02\nPara: David Chen <{DAVID}>\nAsunto: {s.subject}\n\n{block(s)}\n"
HI_GMAIL = lambda s: f"\n\nमंगल, 27 अक्तू॰ 2026 को 11:02 am पर Sales <sales@us.example> ने लिखा:\n\n{q(s)}\n"
NONE = lambda s: ""

FORMATS = {k: v for k, v in dict(gmail=GMAIL, gmail_wrap=GMAIL_WRAP, iphone=IPHONE, outlook=OUTLOOK, outlook_bare=OUTLOOK_BARE, outlook_star=OUTLOOK_STAR, orig=ORIG,
                                 outlook_mac=OUTLOOK_MAC, outlook_ios=OUTLOOK_IOS, samsung=SAMSUNG, yahoo=YAHOO, thunderbird=THUNDERBIRD, proton=PROTON, zimbra=ZIMBRA,
                                 bare_wrote=BARE_WROTE, fwd=FWD, begin_fwd=BEGIN_FWD, es_gmail=ES_GMAIL, fr_gmail=FR_GMAIL, fr_outlook=FR_OUTLOOK, es_outlook=ES_OUTLOOK,
                                 hi_gmail=HI_GMAIL, none=NONE).items()}


def html_of(text: str, sent, style: str) -> str:
    own = "<br>".join(escape(x) for x in text.splitlines())
    quoted = "<br>".join(escape(x) for x in sent.body.splitlines())
    if style == "gmail":
        return (f'<div dir="ltr">{own}</div><br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">On Tue, Oct 27, 2026 at 11:02 AM Sales &lt;<a href="mailto:sales@us.example">sales@us.example</a>&gt; wrote:<br></div>'
                f'<blockquote class="gmail_quote" style="margin:0 0 0 .8ex">{quoted}</blockquote></div>')
    if style == "outlook":
        return (f'<html><body><div style="font-family:Calibri">{own}</div><div id="appendonsend"></div><hr style="display:inline-block;width:98%">'
                f'<div id="divRplyFwdMsg" dir="ltr"><font face="Calibri"><b>From:</b> Sales &lt;sales@us.example&gt;<br><b>Sent:</b> Tuesday, October 27, 2026 11:02 AM<br>'
                f'<b>To:</b> David Chen &lt;{DAVID}&gt;<br><b>Subject:</b> {escape(sent.subject)}</font><div>&nbsp;</div></div><div>{quoted}</div></body></html>')
    if style == "apple":
        return (f'<html><body dir="auto"><div>{own}</div><div><br><blockquote type="cite">On Oct 27, 2026, at 11:02 AM, Sales &lt;sales@us.example&gt; wrote:<br><br></blockquote></div>'
                f'<blockquote type="cite"><div>{quoted}</div></blockquote></body></html>')
    if style == "word":  # Outlook desktop HTML: From block in a bordered div, MsoNormal paragraphs
        paras = "".join(f'<p class="MsoNormal"><span>{escape(x) or "&nbsp;"}</span></p>' for x in text.splitlines())
        return (f'<html><head><style>p.MsoNormal {{margin:0}}</style></head><body><div class="WordSection1">{paras}<p class="MsoNormal">&nbsp;</p>'
                f'<div style="border-top:solid #E1E1E1 1.0pt"><p class="MsoNormal"><b>From:</b> Sales &lt;sales@us.example&gt; <br><b>Sent:</b> Tuesday, October 27, 2026 11:02 AM<br>'
                f'<b>To:</b> David Chen &lt;{DAVID}&gt;<br><b>Subject:</b> {escape(sent.subject)}</p></div><p class="MsoNormal">&nbsp;</p><p class="MsoNormal">{quoted}</p></div></body></html>')
    raise ValueError(style)


def raw_mail(sent, text, fmt="gmail", frm=None, subject=None, thread=True, headers=None, html=None, cc=None, whole=False) -> bytes:
    """`whole`: `text` is the entire body, placed as written with {Q} for the quote."""
    _n[0] += 1
    m = EmailMessage()
    m["From"], m["To"] = frm or f"David Chen <{DAVID}>", "sales@us.example"
    m["Subject"] = subject if subject is not None else f"Re: {sent.subject}"
    m["Message-ID"] = f"<h{_n[0]}@mail.example>"
    m["Date"] = "Wed, 28 Oct 2026 16:00:00 +0000"
    if cc:
        m["Cc"] = cc
    if thread:
        m["In-Reply-To"] = sent.message_id
        m["References"] = sent.message_id
    for k, v in (headers or {}).items():
        m[k] = v
    if html:
        m.set_content(html_of(text, sent, html), subtype="html")
    else:
        body = text.replace("{Q}", q(sent)).replace("{B}", block(sent)) if whole else text + FORMATS[fmt](sent)
        m.set_content(body)
    return bytes(m)


def queue_followup(ctx, sent) -> int:
    with ctx.db.session() as s:
        f = FollowUp(lead_id=sent.lead_id, contact_id=sent.contact_id, company_id=sent.company_id, channel="email", kind="outreach", step=2, due_at=utcnow(),
                     status="awaiting_approval", subject=f"Re: {sent.subject}", body="A second note", to_email=DAVID)
        s.add(f)
        s.flush()
        return f.id


def observe(ctx, step2: int, addr=DAVID, extra=()) -> dict:
    with ctx.db.session() as s:
        rows = s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()
        r = rows[-1] if rows else None
        fup = s.get(FollowUp, step2)
        lead = s.execute(select(Lead)).scalars().first()
        opp = s.execute(select(Opportunity)).scalars().first()
        tasks = [f.subject for f in s.execute(select(FollowUp).where(FollowUp.channel == "task")).scalars()]
        resp = [(f.status, f.to_email) for f in s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars()]
        alerts = [a.message[:90] for a in s.execute(select(Alert).where(Alert.source == "inbox")).scalars()]
        return dict(cat=r.category if r else None, conf=r.confidence if r else None, status=r.status if r else None, intent=r.intent if r else None,
                    susp=is_suppressed(s, addr), susp_from=is_suppressed(s, r.from_email) if r else None, step2=fup.status, stage=lead.outreach_status, lead_status=lead.status,
                    lead_tied=r.lead_id if r else None, opp=(opp.stage, opp.next_step) if opp else None, tasks=tasks, resp=resp, alerts=alerts,
                    new=(r.new_text if r else None), reasons=(r.reasons if r else None), note=(r.action_note if r else None), rec=(r.recommended_status if r else None),
                    n_rows=len(rows), kind=r.mail_kind if r else None)


def run_one(make_ctx, build, addr=DAVID) -> dict:
    ctx, _s, sent = contacted(make_ctx)
    step2 = queue_followup(ctx, sent)
    raws = build(sent)
    raws = raws if isinstance(raws, list) else [raws]
    for raw in raws:
        FakeIMAP.messages[max(FakeIMAP.messages, default=0) + 1] = raw
        run_inbox(ctx)
    out = observe(ctx, step2, addr)
    # what must never survive in the text shown on Replies
    b = sent.body
    line = next(x for x in b.splitlines() if len(x.strip()) > 30)
    out["quote_leak"] = bool(out["new"] and (line.strip() in out["new"] or "to stop receiving" in out["new"].lower() or "sales@us.example" in out["new"].lower()))
    return out
