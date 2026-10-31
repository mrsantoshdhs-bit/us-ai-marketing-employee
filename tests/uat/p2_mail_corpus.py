"""Hand-written mail faithful to what real systems emit, for tests/uat/test_p2_mail.py. Synthetic data only.

Every builder takes v = namespace(to, dom, mid, subj, own, own_dom): the address we wrote to, its domain, the Message-ID and
subject of the email we sent, and the sales mailbox address and domain."""
from __future__ import annotations

import base64
import quopri

DATE = "Sat, 31 Oct 2026 14:10:00 +0000"
_n = 0


def _id(tag):
    global _n
    _n += 1
    return f"<{tag}-{_n}@corpus.example>"


def msg(frm, subj, body="", v_irt=None, refs=None, extra=(), ctype='text/plain; charset="UTF-8"', cte=None, enc="utf-8", to=None,
        mid=None, tag="m"):
    h = ["Return-Path: <x@corpus.example>", "Received: from a.example by mail.own.example; " + DATE, f"From: {frm}",
         f"To: {to or 'sales@own.example'}", f"Subject: {subj}", f"Date: {DATE}", f"Message-ID: {mid or _id(tag)}"]
    if v_irt:
        h.append(f"In-Reply-To: {v_irt}")
    if refs:
        h.append(f"References: {refs}")
    h += list(extra)
    h += ["MIME-Version: 1.0", f"Content-Type: {ctype}"]
    if cte:
        h.append(f"Content-Transfer-Encoding: {cte}")
    head = "\r\n".join(h).encode("utf-8")
    payload = body if isinstance(body, bytes) else body.replace("\r\n", "\n").replace("\n", "\r\n").encode(enc)
    return head + b"\r\n\r\n" + payload


def mp(subtype, parts, boundary, params=""):
    """(content-type value, body bytes) of a multipart built by hand."""
    out = b""
    for ptype, pbody, *more in parts:
        h = [f"Content-Type: {ptype}"] + list(more)
        pb = pbody if isinstance(pbody, bytes) else pbody.replace("\r\n", "\n").replace("\n", "\r\n").encode()
        out += f"--{boundary}\r\n".encode() + "\r\n".join(h).encode() + b"\r\n\r\n" + pb + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f'multipart/{subtype}; {params}boundary="{boundary}"', out


def original(v):
    return (f"Return-path: <{v.own}>\nReceived: from [10.0.0.2] by mail.{v.own_dom}\nMessage-ID: {v.mid}\nFrom: Sales <{v.own}>\n"
            f"To: {v.to}\nSubject: {v.subj}\nDate: Fri, 30 Oct 2026 17:09:00 +0000\nMIME-Version: 1.0\n"
            f'Content-Type: text/plain; charset="utf-8"\n\nHello,\n\nA short note about manual work in your operations.\n\n---\n'
            f'To stop receiving emails from us, reply with "unsubscribe" or write to {v.own}.\nOur company, 1 Main St\n')


def quoted(v):
    return ("> Hello,\n>\n> A short note about manual work in your operations.\n>\n> ---\n"
            f'> To stop receiving emails from us, reply with "unsubscribe" or write to {v.own}.\n')


def q_gmail(v):
    return f"\nOn Fri, Oct 30, 2026 at 1:09 PM Sales <{v.own}> wrote:\n\n{quoted(v)}"


def q_outlook(v):
    return (f"\n________________________________\nFrom: Sales <{v.own}>\nSent: Friday, October 30, 2026 1:09 PM\nTo: {v.to}\n"
            f"Subject: {v.subj}\n\nHello,\n\nA short note about manual work in your operations.\n\n---\n"
            f'To stop receiving emails from us, reply with "unsubscribe" or write to {v.own}.\n')


# ---------------------------------------------------------------- bounces
EXIM_TOP = ("This message was created automatically by mail delivery software.\n\nA message that you sent could not be delivered "
            "to one or more of its\nrecipients. This is a permanent error. The following address(es) failed:\n\n")
USER_UNKNOWN = ("SMTP error from remote mail server after RCPT TO:<{to}>:\n    host mx1.{dom} [192.0.2.25]: 550 5.1.1 <{to}>: "
                "Recipient address rejected: User unknown")


def exim_plain(v, why=USER_UNKNOWN):
    why = why.format(to=v.to, dom=v.dom, own_dom=v.own_dom)
    body = f"{EXIM_TOP}  {v.to}\n    {why}\n\n------ This is a copy of the message, including all the headers. ------\n\n{original(v)}"
    return msg(f"Mail Delivery System <Mailer-Daemon@server1.{v.own_dom}>", "Mail delivery failed: returning message to sender", body,
               extra=[f"X-Failed-Recipients: {v.to}", "Auto-Submitted: auto-replied"], to=v.own, tag="exim")


def exim_dsn(v, status="5.0.0", diag="smtp; 550 5.1.1 <{to}>: Recipient address rejected: User unknown"):
    diag = diag.format(to=v.to, own_dom=v.own_dom)
    text = f"{EXIM_TOP}  {v.to}\n    host mx1.{v.dom} [192.0.2.25]\n    {diag}\n"
    report = (f"Reporting-MTA: dns; server1.{v.own_dom}\n\nAction: failed\nFinal-Recipient: rfc822;{v.to}\nStatus: {status}\n"
              f"Remote-MTA: dns; mx1.{v.dom}\nDiagnostic-Code: {diag}\n")
    ctype, body = mp("report", [('text/plain; charset="us-ascii"', text, "Content-Description: Notification"),
                                ("message/delivery-status", report, "Content-Description: Delivery report"),
                                ("message/rfc822", original(v), "Content-Description: Undelivered Message")],
                     "1730383800-eximdsn-1804289383", "report-type=delivery-status; ")
    return msg(f"Mail Delivery System <Mailer-Daemon@server1.{v.own_dom}>", "Mail delivery failed: returning message to sender", body,
               extra=[f"X-Failed-Recipients: {v.to}", "Auto-Submitted: auto-replied"], ctype=ctype, to=v.own, tag="eximdsn")


def exim_delay_plain(v):
    body = ("This message was created automatically by mail delivery software.\nA message that you sent has not yet been delivered "
            f"to one or more of its\nrecipients after more than 24 hours on the queue on server1.{v.own_dom}.\n\n"
            f"The message identifier is:     1wXyZ-000AbC-12\nThe subject of the message is: {v.subj}\n"
            f"The date of the message is:    Fri, 30 Oct 2026 13:09:00 -0400\n\nThe address to which the message has not yet been "
            f"delivered is:\n\n  {v.to}\n    Delay reason: SMTP error from remote mail server after RCPT TO:<{v.to}>:\n"
            f"    host mx1.{v.dom} [192.0.2.25]: 451 4.7.1 Greylisting in action, please come back later\n\n"
            "No action is required on your part. Delivery attempts will continue for\nsome time, and this warning may be repeated at "
            "intervals if the message\nremains undelivered. Eventually the mail delivery software will give up,\nand when that "
            "happens, the message will be returned to you.\n")
    return msg(f"Mail Delivery System <Mailer-Daemon@server1.{v.own_dom}>", "Warning: message 1wXyZ-000AbC-12 delayed 24 hours", body,
               extra=[f"X-Failed-Recipients: {v.to}", "Auto-Submitted: auto-replied"], to=v.own, tag="eximdelay")


def exim_delay_dsn(v):
    report = (f"Reporting-MTA: dns; server1.{v.own_dom}\n\nAction: delayed\nFinal-Recipient: rfc822;{v.to}\nStatus: 4.0.0\n"
              f"Remote-MTA: dns; mx1.{v.dom}\nDiagnostic-Code: smtp; 451 4.7.1 Greylisting in action\n"
              "Will-Retry-Until: Fri, 06 Nov 2026 17:09:00 +0000\n")
    text = (f"A message that you sent has not yet been delivered to one or more of its\nrecipients after more than 24 hours.\n\n  {v.to}\n"
            "    Delay reason: 451 4.7.1\n\nNo action is required on your part.\n")
    ctype, body = mp("report", [('text/plain; charset="us-ascii"', text), ("message/delivery-status", report),
                                ("text/rfc822-headers", original(v).split("\n\n")[0])],
                     "1730383801-eximdsn-1", "report-type=delivery-status; ")
    return msg(f"Mail Delivery System <Mailer-Daemon@server1.{v.own_dom}>", "Warning: message 1wXyZ-000AbC-12 delayed 24 hours", body,
               ctype=ctype, extra=["Auto-Submitted: auto-replied"], to=v.own, tag="eximdelaydsn")


def postfix_dsn(v, with_id=True):
    text = (f"This is the mail system at host mail.{v.own_dom}.\n\nI'm sorry to have to inform you that your message could not\n"
            "be delivered to one or more recipients. It's attached below.\n\nFor further assistance, please send mail to postmaster.\n\n"
            f"                   The mail system\n\n<{v.to}>: host mx1.{v.dom}[192.0.2.25] said: 550 5.1.1\n    <{v.to}>: Recipient "
            "address rejected: User unknown in virtual mailbox table\n    (in reply to RCPT TO command)\n")
    report = (f"Reporting-MTA: dns; mail.{v.own_dom}\nX-Postfix-Queue-ID: 4Xy1234\nX-Postfix-Sender: rfc822; {v.own}\n"
              f"Arrival-Date: Fri, 30 Oct 2026 17:09:00 +0000 (UTC)\n\nFinal-Recipient: rfc822; {v.to}\nOriginal-Recipient: rfc822;{v.to}\n"
              f"Action: failed\nStatus: 5.1.1\nRemote-MTA: dns; mx1.{v.dom}\nDiagnostic-Code: smtp; 550 5.1.1 <{v.to}>: Recipient address rejected\n")
    headers = original(v).split("\n\n")[0]
    if not with_id:
        headers = "\n".join(line for line in headers.split("\n") if not line.startswith("Message-ID"))
    ctype, body = mp("report", [("text/plain; charset=us-ascii", text, "Content-Description: Notification"),
                                ("message/delivery-status", report, "Content-Description: Delivery report"),
                                ("text/rfc822-headers", headers, "Content-Description: Undelivered Message Headers")],
                     "4Xy1234.1730300000/mail.own", "report-type=delivery-status; ")
    return msg(f"MAILER-DAEMON@mail.{v.own_dom} (Mail Delivery System)", "Undelivered Mail Returned to Sender", body,
               extra=["Auto-Submitted: auto-replied"], ctype=ctype, to=v.own, tag="postfix")


def postfix_delay(v):
    text = (f"This is the mail system at host mail.{v.own_dom}.\n\n# THIS IS A WARNING ONLY.  YOU DO NOT NEED TO RESEND YOUR MESSAGE. #\n\n"
            f"Your message could not be delivered for more than 3 hour(s).\nIt will be retried until it is 5 day(s) old.\n\n<{v.to}>: "
            f"connect to mx1.{v.dom}[192.0.2.25]:25: Connection timed out\n")
    report = (f"Reporting-MTA: dns; mail.{v.own_dom}\n\nFinal-Recipient: rfc822; {v.to}\nAction: delayed\nStatus: 4.4.1\n"
              "Will-Retry-Until: Wed, 4 Nov 2026 17:09:00 +0000\n")
    ctype, body = mp("report", [("text/plain; charset=us-ascii", text), ("message/delivery-status", report),
                                ("text/rfc822-headers", original(v).split("\n\n")[0])],
                     "4Xy9.1730300000/mail.own", "report-type=delivery-status; ")
    return msg(f"MAILER-DAEMON@mail.{v.own_dom} (Mail Delivery System)", "Delayed Mail (still being retried)", body,
               extra=["Auto-Submitted: auto-replied"], ctype=ctype, to=v.own, tag="pfdelay")


def gmail_dsn(v, status="5.1.1", action="failed", subject="Delivery Status Notification (Failure)"):
    text = (f"** Address not found **\n\nYour message wasn't delivered to {v.to} because the address couldn't be found, or is unable "
            "to receive mail.\n\nThe response from the remote server was:\n550 5.1.1 The email account that you tried to reach does not exist.\n")
    if action == "delayed":
        text = (f"** Message not delivered yet **\n\nThere was a temporary problem delivering your message to {v.to}. Gmail will retry "
                "for 46 more hours. You'll be notified if the delivery fails permanently.\n\nThe response from the remote server was:\n"
                "451 4.3.0 Temporary lookup failure\n")
    html = f"<html><head><title>Mail delivery</title></head><body><h1>{text.splitlines()[0]}</h1><p>{text.splitlines()[2]}</p></body></html>"
    alt_t, alt_b = mp("alternative", [('text/plain; charset="UTF-8"', text), ('text/html; charset="UTF-8"', html)], "000000000000alt")
    rel_t, rel_b = mp("related", [(alt_t, alt_b)], "000000000000rel")
    report = (f"Reporting-MTA: dns; googlemail.com\nReceived-From-MTA: dns; {v.own}\nArrival-Date: Fri, 30 Oct 2026 10:09:00 -0700 (PDT)\n"
              f"X-Original-Message-ID: {v.mid}\n\nFinal-Recipient: rfc822; {v.to}\nAction: {action}\nStatus: {status}\n"
              "Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.\n"
              "Last-Attempt-Date: Fri, 30 Oct 2026 10:09:02 -0700 (PDT)\n")
    ctype, body = mp("report", [(rel_t, rel_b), ("message/delivery-status", report), ("message/rfc822", original(v))],
                     "000000000000rep", "report-type=delivery-status; ")
    return msg("Mail Delivery Subsystem <mailer-daemon@mx.google.example>", subject, body, v_irt=v.mid, refs=v.mid,
               extra=[f"X-Failed-Recipients: {v.to}", "Auto-Submitted: auto-replied"], ctype=ctype, to=v.own, tag="gmaildsn")


EXCHANGE = "Microsoft Outlook <MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@contoso.onmicrosoft.com>"


def m365_ndr(v, sender=None, status="5.1.10", action="failed", subject=None, auto=True):
    who = sender or EXCHANGE
    text = (f"Your message to {v.to} couldn't be delivered.\n{v.to.split('@')[0]} wasn't found at {v.dom}.\nAction Required\nRecipient\n"
            "Unknown To address\n\nHow to Fix It\nThe address may be misspelled or may not exist.\n\nDiagnostic information for "
            f"administrators:\nGenerating server: CO1PR01MB1234.prod.exchangelabs.com\n{v.to}\nRemote server returned '550 5.1.10 "
            "RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup'\n")
    if action == "delayed":
        text = (f"Delivery is delayed to these recipients or groups:\n{v.to}\nSubject: {v.subj}\nThis message hasn't been delivered yet. "
                "Delivery will continue to be attempted.\nThe server will keep trying to deliver this message for the next 1 days, "
                "19 hours and 57 minutes. You'll be notified if the message can't be delivered by that time.\n")
    html = "<html><body>" + text.replace("\n", "<br>") + "</body></html>"
    alt_t, alt_b = mp("alternative", [('text/plain; charset="us-ascii"', text), ('text/html; charset="us-ascii"', html)], "alt_ms")
    report = (f"Reporting-MTA: dns;CO1PR01MB1234.prod.exchangelabs.com\nReceived-From-MTA: dns;mail.{v.own_dom}\n"
              f"Arrival-Date: Fri, 30 Oct 2026 17:09:03 +0000\n\nOriginal-Recipient: rfc822;{v.to}\nFinal-Recipient: rfc822;{v.to}\n"
              f"Action: {action}\nStatus: {status}\nDiagnostic-Code: smtp;550 5.1.10 RESOLVER.ADR.RecipientNotFound\n")
    ctype, body = mp("report", [(alt_t, alt_b), ("message/delivery-status", report), ("message/rfc822", original(v))],
                     "ndr_ms", "report-type=delivery-status; ")
    extra = ["Thread-Topic: " + v.subj, "X-MS-Exchange-Message-Is-Ndr:"] + (["Auto-Submitted: auto-replied"] if auto else [])
    return msg(who, subject or f"Undeliverable: {v.subj}", body, v_irt=v.mid, refs=v.mid, extra=extra, ctype=ctype, to=v.own, tag="ndr")


def mimecast_plain_notice(v):
    """A gateway's own plain notice (no DSN parts), sent as postmaster of the prospect's domain."""
    body = (f"Your message could not be delivered.\n\nSubject: {v.subj}\nRecipient: {v.to}\n\nReason: Envelope blocked - User Entry - the "
            "sender's address is blocked by the recipient's administrator.\nError code: 550 Administrative prohibition\n")
    return msg(f"Postmaster <postmaster@{v.dom}>", "Message Delivery Failure - Undeliverable", body, to=v.own, tag="mimecast")


def proofpoint_third_party(v):
    """A filtering service's notice from its own domain, with no thread headers."""
    body = (f"Proofpoint Essentials\n\nThe message you sent to {v.to} was rejected by the recipient's email security policy.\n"
            f"Subject: {v.subj}\nReason: 554 5.7.0 Blocked - see https://ipcheck.proofpoint.com/?ip=192.0.2.9\n")
    return msg("Proofpoint Essentials <noreply@proofpointessentials.example>", f"Message blocked: {v.subj}", body, to=v.own, tag="pp")


def qmail(v):
    body = (f"Hi. This is the qmail-send program at mx1.{v.dom}.\nI'm afraid I wasn't able to deliver your message to the following "
            f"addresses.\nThis is a permanent error; I've given up. Sorry it didn't work out.\n\n<{v.to}>:\nSorry, no mailbox here by that "
            f"name. (#5.1.1)\n\n--- Below this line is a copy of the message.\n\n{original(v)}")
    return msg(f"MAILER-DAEMON@mx1.{v.dom}", "failure notice", body, to=v.own, tag="qmail")


def yahoo(v):
    body = (f"Sorry, we were unable to deliver your message to the following address.\n\n<{v.to}>:\n554: delivery error: dd This user "
            f"doesn't have a {v.dom} account ({v.to}) [0] - mta4010.mail.example\n\n--- Below this line is a copy of the message.\n\n{original(v)}")
    return msg("MAILER-DAEMON@mta.yahoo-mail.example", "Failure Notice", body, to=v.own, tag="yahoo")


def sendmail_no_domain(v):
    body = (f"The original message was received at Fri, 30 Oct 2026 13:09:00 -0400\nfrom localhost\n\n   ----- The following addresses had "
            f"permanent fatal errors -----\n{v.to}\n    (reason: 550 5.1.1 User unknown)\n\n   ----- Transcript of session follows -----\n"
            f"... while talking to mx1.{v.dom}.:\n>>> RCPT To:<{v.to}>\n<<< 550 5.1.1 User unknown\n550 5.1.1 {v.to}... User unknown\n")
    return msg("Mail Delivery Subsystem <MAILER-DAEMON>", "Returned mail: see transcript for details", body, to=v.own, tag="sendmail")


# ---------------------------------------------------------------- automatic replies
def outlook_ooo(v):
    return msg(f"Pat Doe <{v.to}>", f"Automatic reply: {v.subj}", "I am out of the office until Monday, November 2 with limited access to "
               "email.\n\nFor urgent matters call the front desk.\n", v_irt=v.mid, refs=v.mid,
               extra=["Auto-Submitted: auto-generated", "X-Auto-Response-Suppress: All", "Thread-Topic: " + v.subj], tag="oof")


def gmail_vacation(v):
    return msg(f"Pat Doe <{v.to}>", f"Re: {v.subj}", "Thanks for your note. I'm travelling this week and will answer when I'm back.\n",
               v_irt=v.mid, refs=v.mid, extra=["Auto-Submitted: auto-replied", "Precedence: bulk", "X-Autoreply: yes"], tag="vac")


def no_longer_monitored(v):
    return msg(f"Acme Office <{v.to}>", f"Automatic reply: {v.subj}", f"This mailbox is no longer monitored. Please write to jane.smith@{v.dom} "
               "for all enquiries.\n", v_irt=v.mid, refs=v.mid, extra=["Auto-Submitted: auto-generated"], tag="nomon")


def left_company(v):
    return msg(f"Pat Doe <{v.to}>", f"Automatic reply: {v.subj}", f"Pat Doe has left the company and this address will be closed. For new "
               f"business please contact Jane Smith, jane.smith@{v.dom}.\n", v_irt=v.mid, refs=v.mid,
               extra=["Auto-Submitted: auto-replied", "X-Autoreply: yes"], tag="left")


def left_company_no_header(v):
    return msg(f"Pat Doe <{v.to}>", f"Re: {v.subj}", f"Pat Doe no longer works at this company. Please contact jane.smith@{v.dom} instead.\n",
               v_irt=v.mid, refs=v.mid, tag="left2")


ZD_FOOT = "This email is a service from Acme Support. Delivered by Zendesk\n"


def zendesk_confirm(v, thread=True):
    return msg("Acme Support <support@acme.zendesk.example>", f"[Request received] {v.subj}", "##- Please type your reply above this line -##"
               f"\n\nYour request (#123) has been received and is being reviewed by our support staff.\n\nTo add additional comments, reply to this email.\n\n{ZD_FOOT}",
               v_irt=v.mid if thread else None, refs=v.mid if thread else None,
               extra=["Auto-Submitted: auto-generated", "X-Mailer: Zendesk Mailer", "X-Auto-Response-Suppress: All",
                      "Reply-To: Acme Support <support+id123@acme.zendesk.example>"], tag="zd")


def freshdesk_confirm(v):
    return msg("Acme Helpdesk <support@acme.freshdesk.example>", f"Ticket Received - {v.subj}", "Dear customer,\n\nWe would like to acknowledge "
               "that we have received your request and a ticket has been created.\nTicket ID: #123\nA support representative will review your "
               "request and send you a personal response shortly.\n", tag="fd")


def helpdesk_agent_interest(v, thread=True, auto=True, frm=None):
    extra = ["X-Mailer: Zendesk Mailer", "Reply-To: Acme Support <support+id123@acme.zendesk.example>"] + (["Auto-Submitted: auto-generated"] if auto else [])
    return msg(frm or f"Jane Smith (Acme Support) <support@{v.dom}>", f"[Acme] Re: {v.subj}", "##- Please type your reply above this line -##\n\n"
               f"Jane Smith, Oct 31, 10:02 EDT\n\nHi, yes this sounds interesting. Can we talk Thursday?\n\n{ZD_FOOT}",
               v_irt=v.mid if thread else None, refs=v.mid if thread else None, extra=extra, tag="zdagent")


def helpdesk_agent_foreign_domain(v):
    return msg("Jane Smith (Acme Support) <support@acme.zendesk.example>", f"[Acme] Re: {v.subj}", "Hi, yes this sounds interesting. Can we talk Thursday?\n",
               v_irt="<ABCD1234_sprut@zendesk.example>", refs="<ABCD1234_sprut@zendesk.example>",
               extra=["X-Mailer: Zendesk Mailer", "Auto-Submitted: auto-generated"], tag="zdagent2")


def shared_inbox_tool(v):
    """Front / Help Scout style: a person answers from a tool, the tool's domain in From, thread headers replaced."""
    return msg("Jane Smith <hello@acme.frontapp.example>", f"Re: {v.subj}", f"We would like to know more, what does it cost?\n\nOn Fri, Oct 30 2026, "
               f"Sales <{v.own}> wrote:\n{quoted(v)}", v_irt="<front-conv-1@frontapp.example>", extra=["X-Mailer: Front"], tag="front")


def challenge(v):
    return msg("Mail Verification <verify@challenge.example>", f"Re: {v.subj} [please verify]", f"Your message to {v.to} is on hold. This mailbox only "
               "accepts mail from verified senders.\nClick https://challenge.example/v/abc to verify you are human.\n", v_irt=v.mid,
               extra=["Auto-Submitted: auto-replied"], tag="cr")


def challenge_no_thread(v):
    return msg("Mail Verification <verify@challenge.example>", f"Please verify your message to {v.to}", "Click https://challenge.example/v/abc to verify you are human.\n", tag="cr2")


def autoreply_with_company_footer(v):
    return msg(f"Acme Office <{v.to}>", f"Automatic reply: {v.subj}", "Thank you for contacting Acme. We will answer within 2 business days.\n\n"
               "Acme Inc | 1 Harbor Way | Boston MA\nTo unsubscribe from our newsletter click here.\n", v_irt=v.mid, refs=v.mid,
               extra=["Auto-Submitted: auto-generated"], tag="oofoot")


def read_receipt_outlook(v):
    text = (f"Your message\n\n    To: {v.to}\n    Subject: {v.subj}\n    Sent: Friday, October 30, 2026 1:09:00 PM (UTC-05:00)\n\n"
            "was read on Saturday, October 31, 2026 10:00:00 AM (UTC-05:00).\n")
    report = (f"Reporting-UA: outlook.example; Microsoft Outlook 16.0\nFinal-Recipient: rfc822;{v.to}\nOriginal-Message-ID: {v.mid}\n"
              "Disposition: manual-action/MDN-sent-automatically; displayed\n")
    ctype, body = mp("report", [("text/plain; charset=us-ascii", text), ("message/disposition-notification", report)], "mdn1",
                     "report-type=disposition-notification; ")
    return msg(f"Pat Doe <{v.to}>", f"Read: {v.subj}", body, ctype=ctype, tag="mdn")


def read_receipt_thunderbird(v):
    text = (f"This is a Return Receipt for the mail that you sent to {v.to}.\n\nNote: This Return Receipt only acknowledges that the message "
            "was displayed on the recipient's computer. There is no guarantee that the recipient has read or understood the message contents.\n")
    report = (f"Reporting-UA: {v.dom}; Thunderbird 115.0\nFinal-Recipient: rfc822;{v.to}\nOriginal-Message-ID: {v.mid}\n"
              "Disposition: manual-action/MDN-sent-manually; displayed\n")
    ctype, body = mp("report", [("text/plain; charset=UTF-8", text), ("message/disposition-notification", report),
                                ("text/rfc822-headers", original(v).split("\n\n")[0])], "mdn2", "report-type=disposition-notification; ")
    return msg(f"Pat Doe <{v.to}>", f"Return Receipt (displayed) - {v.subj}", body, v_irt=v.mid, refs=v.mid, ctype=ctype, tag="mdn2")


def delivery_receipt(v):
    text = f"Your message has been delivered to the following recipients:\n\n{v.to}\n\nSubject: {v.subj}\n"
    report = f"Reporting-MTA: dns;mx1.{v.dom}\n\nFinal-Recipient: rfc822;{v.to}\nAction: delivered\nStatus: 2.0.0\n"
    ctype, body = mp("report", [("text/plain; charset=us-ascii", text), ("message/delivery-status", report),
                                ("text/rfc822-headers", original(v).split("\n\n")[0])], "dsnok", "report-type=delivery-status; ")
    return msg(EXCHANGE, f"Delivered: {v.subj}", body, v_irt=v.mid, refs=v.mid, ctype=ctype, to=v.own, tag="dsnok")


# ---------------------------------------------------------------- structure (a reply in our thread)
def thr(v, body, subj=None, frm=None, **kw):
    return msg(frm or f"Pat Doe <{v.to}>", subj or f"Re: {v.subj}", body, v_irt=v.mid, refs=v.mid, **kw)


OPT = "Please remove me from your mailing list."
HOT = "Sounds interesting. Can we talk Thursday?"


def html_only(v, text=OPT):
    return thr(v, f'<html><body><div dir="ltr">{text}</div><br><div class="gmail_quote"><div class="gmail_attr">On Fri, Oct 30, 2026 at 1:09 PM Sales '
               f'&lt;{v.own}&gt; wrote:<br></div><blockquote class="gmail_quote">Hello<br>To stop receiving emails from us, reply with "unsubscribe"'
               "</blockquote></div></body></html>", ctype='text/html; charset="UTF-8"', tag="html")


def alt_empty_text(v, text=OPT, plain=""):
    ctype, body = mp("alternative", [('text/plain; charset="UTF-8"', plain),
                                     ('text/html; charset="UTF-8"', f"<html><body><p>{text}</p></body></html>")], "alt-empty")
    return thr(v, body, ctype=ctype, tag="altempty")


def qp(v, text=OPT):
    return thr(v, quopri.encodestring((text + q_gmail(v)).encode()), ctype='text/plain; charset="UTF-8"', cte="quoted-printable", tag="qp")


def b64(v, text=OPT):
    return thr(v, base64.encodebytes((text + q_gmail(v)).encode()), ctype='text/plain; charset="UTF-8"', cte="base64", tag="b64")


def latin1(v):
    return thr(v, "Grüße aus München.\nPlease remove me from your list.\n".encode("latin-1"), ctype='text/plain; charset="ISO-8859-1"', cte="8bit", tag="latin")


def cp1252(v):
    return thr(v, "Please don\x92t contact us again \x96 thanks.\n".encode("latin-1"), ctype='text/plain; charset="Windows-1252"', cte="8bit", tag="cp")


def shift_jis(v):
    return thr(v, "配信停止をお願いします。 Please remove me from your list.\n".encode("shift_jis"), ctype='text/plain; charset="Shift_JIS"', cte="8bit", tag="sjis")


def undeclared_latin1(v):
    return thr(v, "Por favor, quíteme. Please remove me from your list.\n".encode("latin-1"), ctype="text/plain", cte="8bit", tag="undecl")


TNEF = base64.encodebytes(b"\x78\x9f\x3e\x22" + b"\0" * 300)


def tnef_only(v):
    ctype, body = mp("mixed", [('application/ms-tnef; name="winmail.dat"', TNEF, "Content-Transfer-Encoding: base64",
                                'Content-Disposition: attachment; filename="winmail.dat"')], "tnef1")
    return thr(v, body, ctype=ctype, tag="tnef")


def tnef_plain(v, text=OPT):
    ctype, body = mp("mixed", [('text/plain; charset="us-ascii"', text), ('application/ms-tnef; name="winmail.dat"', TNEF,
                     "Content-Transfer-Encoding: base64", 'Content-Disposition: attachment; filename="winmail.dat"')], "tnef2")
    return thr(v, body, ctype=ctype, tag="tnef2")


def iphone(v, text=HOT):
    return thr(v, f"{text}\n\nSent from my iPhone\n\n> On Oct 30, 2026, at 1:09 PM, Sales <{v.own}> wrote:\n>\n{quoted(v)}", tag="ios")


def iphone_first(v, text=OPT):
    return thr(v, f"Sent from my iPhone\n\n{text}\n", tag="ios2")


def bottom_post(v, text=OPT):
    return thr(v, f"On Fri, Oct 30, 2026 at 1:09 PM Sales <{v.own}> wrote:\n\n{quoted(v)}\n{text}\n", tag="bottom")


def wrapped_gmail_header(v, text=HOT):
    return thr(v, f"{text}\n\nOn Fri, Oct 30, 2026 at 1:09 PM Sales Person Name <\n{v.own}> wrote:\n\n{quoted(v)}", tag="wrap")


def outlook_top(v, text=HOT):
    return thr(v, text + "\n" + q_outlook(v), tag="outlook")


def _plain_copy(v):
    return f'Hello,\nA short note.\n---\nTo stop receiving emails from us, reply with "unsubscribe" or write to {v.own}.\n'


def outlook_de(v, text=OPT):
    return thr(v, f"{text}\n\nVon: Sales <{v.own}>\nGesendet: Freitag, 30. Oktober 2026 18:09\nAn: {v.to}\nBetreff: {v.subj}\n\n{_plain_copy(v)}", tag="de")


def outlook_es(v, text=HOT):
    return thr(v, f"{text}\n\nDe: Sales\nEnviado: viernes, 30 de octubre de 2026 13:09\nPara: {v.to}\nAsunto: {v.subj}\n\n{_plain_copy(v)}", tag="es")


def outlook_fr(v, text=HOT):
    return thr(v, f"{text}\n\nDe : Sales\nEnvoyé : vendredi 30 octobre 2026 18:09\nÀ : {v.to}\nObjet : {v.subj}\n\n{_plain_copy(v)}", tag="fr")


def gmail_es(v, text=HOT):
    return thr(v, f"{text}\n\nEl vie, 30 oct 2026 a las 13:09, Sales (<{v.own}>) escribió:\n\n{quoted(v)}", tag="esg")


def gmail_fr(v, text=HOT):
    return thr(v, f"{text}\n\nLe ven. 30 oct. 2026 à 18:09, Sales <{v.own}> a écrit :\n\n{quoted(v)}", tag="frg")


def gmail_de(v, text=HOT):
    return thr(v, f"{text}\n\nAm Fr., 30. Okt. 2026 um 18:09 Uhr schrieb Sales <{v.own}>:\n\n{quoted(v)}", tag="deg")


def apple_de(v, text=HOT):
    return thr(v, f"{text}\n\nVon meinem iPhone gesendet\n\n> Am 30.10.2026 um 18:09 schrieb Sales <{v.own}>:\n>\n{quoted(v)}", tag="dea")


def huge_attachment(v, text=OPT, mb=30):
    ctype, body = mp("mixed", [('text/plain; charset="UTF-8"', text), ('application/pdf; name="big.pdf"', base64.encodebytes(b"%PDF" * (mb * 262144)),
                     "Content-Transfer-Encoding: base64", 'Content-Disposition: attachment; filename="big.pdf"')], "huge")
    return thr(v, body, ctype=ctype, tag="huge")


def mb_body_plain(v, text=OPT):
    return thr(v, text + "\n\n" + ("Lorem ipsum dolor sit amet consectetur adipiscing elit.\n" * 19000), tag="1mb")


def mb_body_html(v, text=HOT):
    return thr(v, f"<html><body><p>{text}</p>" + "<div><span style='color:red'>lorem ipsum dolor sit amet</span></div>" * 16000 + "</body></html>",
               ctype='text/html; charset="UTF-8"', tag="1mbh")


def mb_body_nested_quotes(v, text=HOT):
    return thr(v, f"<html><body><p>{text}</p>" + "<blockquote>x " * 6000 + "</blockquote>" * 6000 + "<style>" * 3000 + "</body></html>",
               ctype='text/html; charset="UTF-8"', tag="1mbq")


def forward_as_attachment(v):
    """A colleague writes a note and attaches our email as a message/rfc822 part."""
    ctype, body = mp("mixed", [('text/plain; charset="UTF-8"', "Sounds interesting, please call me on Thursday. I am the owner here. Pat forwarded me your note.\n"),
                               ("message/rfc822", original(v), 'Content-Disposition: attachment; filename="note.eml"')], "fwdatt")
    return msg(f"Jane Smith <jane.smith@{v.dom}>", f"Re: {v.subj}", body, ctype=ctype, tag="fwdatt")


def forward_as_attachment_inner(v):
    """The writer is interested; only the mail attached to it (someone else's, inline) speaks of removal."""
    inner = "From: Vendor <v@other.example>\nTo: Pat\nSubject: list\nContent-Type: text/plain\n\nPlease remove me from your mailing list.\n"
    ctype, body = mp("mixed", [('text/plain; charset="UTF-8"', "Sounds interesting, please call me on Thursday.\n"),
                               ("message/rfc822", inner, "Content-Disposition: inline")], "fwdatt2")
    return thr(v, body, ctype=ctype, tag="fwdatt2")


def smime_signed(v, text=OPT):
    ctype, body = mp("signed", [('text/plain; charset="UTF-8"', text), ('application/pkcs7-signature; name="smime.p7s"', base64.encodebytes(b"\x30\x82" * 50),
                     "Content-Transfer-Encoding: base64", 'Content-Disposition: attachment; filename="smime.p7s"')], "smime",
                     'protocol="application/pkcs7-signature"; micalg=sha-256; ')
    return thr(v, body, ctype=ctype, tag="smime")


def gateway_disclaimer(v, text=OPT):
    alt_t, alt_b = mp("alternative", [('text/plain; charset="UTF-8"', text), ('text/html; charset="UTF-8"', f"<p>{text}</p>")], "dalt")
    ctype, body = mp("mixed", [(alt_t, alt_b), ('text/plain; charset="UTF-8"', "This message and any attachments are confidential. If you are not the "
                                                "intended recipient, please delete it and notify the sender.\n")], "dmix")
    return thr(v, body, ctype=ctype, tag="disc")


def caution_banner(v, text=OPT):
    return thr(v, f"CAUTION: This email originated from outside of the organization. Do not click links or open attachments unless you recognize "
               f"the sender.\n\n{text}\n{q_outlook(v)}", tag="banner")


def human_with_marketing_signature(v):
    return thr(v, f"Sounds interesting. Can we talk Thursday?\n\nJane Smith\nCEO, Acme Inc\nwww.{v.dom}\nIf you no longer wish to receive emails from "
               f"Acme, click here to unsubscribe.\n{q_gmail(v)}", frm=f"Jane Smith <jane.smith@{v.dom}>", tag="sig")


def human_sig_newsletter(v):
    return thr(v, f"Yes, please send me more details about this.\n\nRegards,\nPat\nUnsubscribe from our newsletter | Manage your preferences\n{q_outlook(v)}", tag="sig2")


def disclaimer_only_marketing(v):
    return thr(v, "Not interested right now but let us talk next quarter.\n\nThis email was sent by Acme Inc. You may opt out of Acme marketing at any time.\n", tag="sig3")


# ---------------------------------------------------------------- non-English
LANG = {
    "es_opt1": "Por favor, elimínenme de su lista de correo.",
    "es_opt2": "No me envíen más correos, gracias.",
    "es_opt3": "Quiero darme de baja.",
    "de_opt1": "Bitte nehmen Sie mich aus Ihrem Verteiler.",
    "de_opt2": "Bitte keine weiteren E-Mails.",
    "de_opt3": "Abmelden bitte.",
    "fr_opt1": "Merci de me retirer de votre liste.",
    "fr_opt2": "Je souhaite me désabonner.",
    "fr_opt3": "Ne m'envoyez plus d'e-mails.",
    "hi_opt1": "Kripya mujhe email mat bhejiye.",
    "hi_opt2": "Mujhe list se hata do.",
    "hi_opt3": "Email bhejna band karo.",
    "es_hot": "Me interesa, ¿podemos hablar el jueves?",
    "de_hot": "Ja, gerne. Können wir am Donnerstag telefonieren?",
    "fr_hot": "Oui, cela m'intéresse. On peut en parler jeudi ?",
    "hi_hot": "Haan, interested hain. Kal call kar lete hain.",
    "es_no": "No estamos interesados, gracias.",
    "de_no": "Kein Interesse, danke.",
    "fr_no": "Pas intéressé, merci.",
    "hi_no": "Humein zaroorat nahi hai, dhanyavaad.",
}


def lang_de_ooo_noheader(v):
    return msg(f"Pat Doe <{v.to}>", f"Abwesenheitsnotiz: {v.subj}", "Ich bin bis zum 5. November nicht im Büro.\n", v_irt=v.mid, refs=v.mid, tag="deooo")


def lang_es_ooo_header(v):
    return msg(f"Pat Doe <{v.to}>", f"Respuesta automática: {v.subj}", "Estoy fuera de la oficina hasta el lunes.\n", v_irt=v.mid, refs=v.mid,
               extra=["Auto-Submitted: auto-replied"], tag="esooo")


# ---------------------------------------------------------------- calendar and Calendly
def ics(v, method="REQUEST"):
    return (f"BEGIN:VCALENDAR\nPRODID:-//Google Inc//Google Calendar 70.9054//EN\nVERSION:2.0\nMETHOD:{method}\nBEGIN:VEVENT\nDTSTART:20261105T150000Z\n"
            f"DTEND:20261105T153000Z\nORGANIZER;CN=Pat Doe:mailto:{v.to}\nATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{v.own}\n"
            "SUMMARY:Intro call\nUID:abc@google.com\nEND:VEVENT\nEND:VCALENDAR\n")


INVITE = "You have been invited to the following event.\n\nIntro call\nWhen: Thu Nov 5, 2026 10am - 10:30am (EST)\nGoing? Yes - Maybe - No\n"


def ics_only_in_thread(v):
    return thr(v, ics(v), ctype='text/calendar; charset="UTF-8"; method=REQUEST', tag="ics")


def ics_gmail_invite_in_thread(v):
    alt_t, alt_b = mp("alternative", [('text/plain; charset="UTF-8"', INVITE), ('text/html; charset="UTF-8"', f"<html><body>{INVITE}</body></html>"),
                                      ('text/calendar; charset="UTF-8"; method=REQUEST', ics(v))], "icsalt")
    ctype, body = mp("mixed", [(alt_t, alt_b), ('application/ics; name="invite.ics"', base64.encodebytes(ics(v).encode()), "Content-Transfer-Encoding: base64",
                                'Content-Disposition: attachment; filename="invite.ics"')], "icsmix")
    return thr(v, body, subj=f"Invitation: Intro call @ Thu Nov 5, 2026 10am - 10:30am (EST) ({v.own})", ctype=ctype,
               extra=["Sender: Google Calendar <calendar-notification@google.com>"], tag="icsg")


def ics_invite_colleague_no_thread(v):
    alt_t, alt_b = mp("alternative", [('text/plain; charset="UTF-8"', INVITE), ('text/calendar; charset="UTF-8"; method=REQUEST', ics(v))], "icsalt2")
    return msg(f"Jane Smith <jane.smith@{v.dom}>", "Invitation: Intro call @ Thu Nov 5, 2026 10am - 10:30am (EST)", alt_b, ctype=alt_t, tag="icsc")


def ics_accept(v):
    alt_t, alt_b = mp("alternative", [('text/plain; charset="UTF-8"', "Pat Doe has accepted this invitation.\n"),
                                      ('text/calendar; charset="UTF-8"; method=REPLY', ics(v, "REPLY"))], "icsrep")
    return msg(f"Pat Doe <{v.to}>", "Accepted: Intro call @ Thu Nov 5, 2026", alt_b, ctype=alt_t, tag="icsa")


CALENDLY_SUBJECT = "New Event: Pat Doe - 10:00am Thu, Nov 5, 2026 - 30 Minute Meeting"


def _calendly_body(v):
    return (f"---------- Forwarded message ---------\nFrom: Calendly <notifications@calendly.com>\nDate: Sat, Oct 31, 2026 at 10:00 AM\n"
            f"Subject: {CALENDLY_SUBJECT}\nTo: <owner@own.example>\n\nHi,\n\nA new event has been scheduled.\n\nEvent Type:\n30 Minute Meeting\n\n"
            f"Invitee:\nPat Doe\n\nInvitee Email:\n{v.to}\n\nEvent Date/Time:\n10:00am - Thursday, November 5, 2026 (Eastern Time - US & Canada)\n\n"
            "View event in Calendly\nhttps://calendly.com/app/scheduled_events/user/me\n")


def calendly_fwd_colleague(v):
    return msg(f"Jane Smith <jane.smith@{v.dom}>", f"Fwd: {CALENDLY_SUBJECT}", _calendly_body(v), tag="cal1")


def calendly_fwd_owner_gmail(v):
    return msg("Owner <owner.personal@owner-home.example>", f"Fwd: {CALENDLY_SUBJECT}", _calendly_body(v), tag="cal2")


def calendly_fwd_own_mailbox(v):
    return msg(f"Sales <{v.own}>", f"Fwd: {CALENDLY_SUBJECT}", _calendly_body(v), tag="cal3")


def calendly_fwd_wg_prefix(v):
    return msg("Owner <owner.personal@owner-home.example>", f"WG: {CALENDLY_SUBJECT}", _calendly_body(v), tag="cal4")


# ---------------------------------------------------------------- who really answers
def jane(v):
    return f"Jane Smith <jane.smith@{v.dom}>"


GMAIL = "Jane Smith <x@gmail.com>"
NO = "Not interested, thanks."
WHO = {
    "jane_thread_hot": lambda v: msg(jane(v), f"Re: {v.subj}", HOT + q_gmail(v), v_irt=v.mid, refs=v.mid, tag="w1"),
    "jane_refs_only_hot": lambda v: msg(jane(v), f"Re: {v.subj}", HOT + q_gmail(v), refs=f"<other@x.example> {v.mid}", tag="w2"),
    "jane_subject_only_hot": lambda v: msg(jane(v), f"Re: {v.subj}", HOT + q_gmail(v), tag="w3"),
    "jane_subject_only_no": lambda v: msg(jane(v), f"Re: {v.subj}", NO + q_gmail(v), tag="w3b"),
    "jane_subject_only_question": lambda v: msg(jane(v), f"Re: {v.subj}", "Who gave you this address?" + q_gmail(v), tag="w3c"),
    "jane_forward_thread_new_ids": lambda v: msg(jane(v), f"RE: FW: {v.subj}", HOT + "\n" + q_outlook(v), v_irt="<fwd-internal-1@acme.example>",
                                                 refs="<fwd-internal-1@acme.example>", tag="w4"),
    "jane_subdomain_subject_only": lambda v: msg(f"Jane Smith <jane.smith@us.{v.dom}>", f"Re: {v.subj}", HOT + q_gmail(v), tag="w5"),
    "jane_parent_domain_subject_only": lambda v: msg("Jane Smith <jane.smith@acme-holdings.example>", f"RE: FW: {v.subj}", HOT + "\n" + q_outlook(v), tag="w6"),
    "gmail_thread_hot": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT + q_gmail(v), v_irt=v.mid, refs=v.mid, tag="w7"),
    "gmail_refs_only_hot": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT + q_gmail(v), refs=v.mid, tag="w8"),
    "gmail_subject_only_hot": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT + q_gmail(v), tag="w9"),
    "gmail_subject_only_opt": lambda v: msg(GMAIL, f"Re: {v.subj}", OPT + q_gmail(v), tag="w10"),
    "jane_thread_opt": lambda v: msg(jane(v), f"Re: {v.subj}", OPT + q_gmail(v), v_irt=v.mid, refs=v.mid, tag="w11"),
    "jane_subject_only_opt": lambda v: msg(jane(v), f"Re: {v.subj}", OPT + q_gmail(v), tag="w12"),
    "jane_thread_no": lambda v: msg(jane(v), f"Re: {v.subj}", NO + q_gmail(v), v_irt=v.mid, refs=v.mid, tag="w13"),
    "jane_thread_referral": lambda v: msg(jane(v), f"Re: {v.subj}", f"You should speak to our COO, tom@{v.dom}." + q_gmail(v), v_irt=v.mid, refs=v.mid, tag="w14"),
    "irt_with_comment": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT, v_irt=f"{v.mid} (from Sales at Oct 30)", tag="w15"),
    "irt_two_ids": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT, v_irt=f"<a@x.example> {v.mid}", tag="w16"),
    "refs_no_spaces": lambda v: msg(GMAIL, f"Re: {v.subj}", HOT, refs=f"<a@x.example>{v.mid}", tag="w17"),
    "no_from_address_thread": lambda v: msg("undisclosed-recipients:;", f"Re: {v.subj}", OPT, v_irt=v.mid, tag="w18"),
    "noreply_with_reply_to_person": lambda v: msg(f'"Jane Smith via Acme" <noreply@{v.dom}>', f"Re: {v.subj}", HOT, v_irt=v.mid, refs=v.mid,
                                                  extra=[f"Reply-To: jane.smith@{v.dom}"], tag="w19"),
}

BOUNCES = {
    "exim_plain": exim_plain, "exim_plain_unrouteable": lambda v: exim_plain(v, "Unrouteable address"),
    "exim_dsn": exim_dsn, "exim_delay_plain": exim_delay_plain, "exim_delay_dsn": exim_delay_dsn,
    "exim_hourly_limit_plain": lambda v: exim_plain(v, "Domain {own_dom} has exceeded the max emails per hour (100/100 (100%)) allowed.  Message discarded."),
    "exim_hourly_limit_dsn": lambda v: exim_dsn(v, diag="X-Exim; Domain {own_dom} has exceeded the max emails per hour (100/100 (100%)) allowed.  Message discarded."),
    "postfix_dsn": postfix_dsn, "postfix_dsn_no_id": lambda v: postfix_dsn(v, with_id=False), "postfix_delay": postfix_delay,
    "gmail_dsn": gmail_dsn, "gmail_delay": lambda v: gmail_dsn(v, "4.3.0", "delayed", "Delivery Status Notification (Delay)"),
    "m365_ndr_exchange_sender": m365_ndr, "m365_ndr_postmaster": lambda v: m365_ndr(v, f"Mail Delivery <postmaster@{v.dom}>"),
    "m365_delay_noauto": lambda v: m365_ndr(v, status="4.4.7", action="delayed", subject=f"Delivery delayed:{v.subj}", auto=False),
    "m365_delay_auto": lambda v: m365_ndr(v, status="4.4.7", action="delayed", subject=f"Delivery delayed:{v.subj}"),
    "mimecast_block_in_exim_dsn": lambda v: exim_dsn(v, diag="smtp; 550 Administrative prohibition - envelope blocked - https://community.mimecast.com/docs/DOC-1369#550"),
    "proofpoint_ip_block_in_exim_dsn": lambda v: exim_dsn(v, status="5.7.0", diag="smtp; 554 5.7.0 Blocked - see https://ipcheck.proofpoint.com/?ip=192.0.2.9"),
    "mimecast_plain_notice_postmaster": mimecast_plain_notice, "proofpoint_third_party_notice": proofpoint_third_party,
    "qmail": qmail, "yahoo": yahoo, "sendmail_no_domain": sendmail_no_domain,
}

AUTO = {
    "outlook_ooo": outlook_ooo, "gmail_vacation": gmail_vacation, "no_longer_monitored": no_longer_monitored, "left_company": left_company,
    "left_company_no_header": left_company_no_header, "zendesk_confirm_thread": zendesk_confirm, "zendesk_confirm_no_thread": lambda v: zendesk_confirm(v, thread=False),
    "freshdesk_confirm_no_thread": freshdesk_confirm, "helpdesk_agent_hot_auto_header": helpdesk_agent_interest,
    "helpdesk_agent_hot_company_domain_no_thread": lambda v: helpdesk_agent_interest(v, thread=False, auto=False),
    "helpdesk_agent_foreign_domain": helpdesk_agent_foreign_domain, "shared_inbox_tool": shared_inbox_tool, "challenge_thread": challenge,
    "challenge_no_thread": challenge_no_thread, "autoreply_company_footer": autoreply_with_company_footer, "read_receipt_outlook": read_receipt_outlook,
    "read_receipt_thunderbird": read_receipt_thunderbird, "delivery_receipt": delivery_receipt,
    "human_hot_precedence_bulk": lambda v: thr(v, HOT, extra=["Precedence: bulk"], tag="prec"),
}

STRUCTURE = {
    "html_only_opt": html_only, "alt_empty_text_opt": alt_empty_text, "alt_empty_text_hot": lambda v: alt_empty_text(v, HOT), "alt_space_text_opt": lambda v: alt_empty_text(v, OPT, " \n"),
    "qp_opt": qp, "b64_opt": b64, "latin1": latin1, "cp1252": cp1252, "shift_jis": shift_jis, "undeclared_latin1": undeclared_latin1, "tnef_only": tnef_only, "tnef_plain": tnef_plain,
    "iphone_hot": iphone, "iphone_first_opt": iphone_first, "bottom_post_opt": bottom_post, "wrapped_gmail_header_hot": wrapped_gmail_header, "outlook_top_hot": outlook_top,
    "outlook_de_opt": outlook_de, "outlook_es_hot": outlook_es, "outlook_fr_hot": outlook_fr, "gmail_es_hot": gmail_es, "gmail_fr_hot": gmail_fr, "gmail_de_hot": gmail_de, "apple_de_hot": apple_de,
    "huge_attachment_opt": huge_attachment, "mb_body_plain_opt": mb_body_plain, "mb_body_html_hot": mb_body_html, "mb_body_nested_quotes": mb_body_nested_quotes,
    "forward_as_attachment": forward_as_attachment, "forward_as_attachment_inner": forward_as_attachment_inner, "smime_signed_opt": smime_signed, "gateway_disclaimer_opt": gateway_disclaimer,
    "caution_banner_opt": caution_banner, "human_marketing_signature_hot": human_with_marketing_signature, "human_sig_newsletter": human_sig_newsletter,
    "human_objection_opt_out_disclaimer": disclaimer_only_marketing,
}

CALENDAR = {
    "ics_only_in_thread": ics_only_in_thread, "ics_gmail_invite_in_thread": ics_gmail_invite_in_thread, "ics_invite_colleague_no_thread": ics_invite_colleague_no_thread,
    "ics_accept_no_thread": ics_accept, "calendly_fwd_colleague": calendly_fwd_colleague, "calendly_fwd_owner_gmail": calendly_fwd_owner_gmail,
    "calendly_fwd_own_mailbox": calendly_fwd_own_mailbox, "calendly_fwd_wg_prefix": calendly_fwd_wg_prefix,
}


def corpus() -> dict:
    def answer(t):
        return lambda v: thr(v, t + q_gmail(v), tag="lang")

    lang = {k: answer(t) for k, t in LANG.items()}
    lang |= {"de_ooo_noheader": lang_de_ooo_noheader, "es_ooo_header": lang_es_ooo_header}
    out = {}
    for group, d in (("who", WHO), ("bounce", BOUNCES), ("auto", AUTO), ("struct", STRUCTURE), ("lang", lang), ("cal", CALENDAR)):
        for k, fn in d.items():
            out[f"{group}.{k}"] = fn
    return out
