"""Inbox Agent: reads the sales mailbox (read-only), classifies replies, updates the CRM, suggests responses.

Without IMAP settings it still processes simulated replies (clearly labelled) so the workflow can be
tested; real mailbox reading starts once IMAP_HOST and the credentials are set.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
from collections.abc import Collection
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.agents.sales_workflow import SIGN_IN_ALERT
from aime.db import kv_get, kv_set
from aime.dedupe import normalize_name
from aime.inbox import (BODY_LIMIT, BOUNCE_SUBJECT, HEADER_LIMIT, NO_ADDRESS, MailSignInRefused, Mailbox, ParsedEmail, large_note, new_text,
                        parse_headers, parse_message)
from aime.mail_auth import sender_signed
from aime.meetings import check_double_bookings
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, SkippedMail
from aime.monitoring import alert_once, resolve_alerts
from aime.reply_intel import (BOOKING_NOTICE_DAYS, CALENDLY_NOTICE, CALENDLY_SUBJECT, JUNK_ALERT, NO_REPLY, REPLY_WORDS, UNSUBSCRIBE_SUBJECT,
                              booking_links_without_notice, calendly_sender, company_leads, forget_old_skipped_mail, process_pending, remember_sent_subjects,
                              remembered_lead_id, sent_with_our_subject, subdomain_leads)
from aime.timeutil import utcnow
from aime.website_check import GENERIC_TOKENS


NOTICE_ALERT = "calendly:no_notices"
READ_TRIES = 3  # an email that every run dies on is met this many times, then set aside so the mail behind it is read
REFUSED_TICKS = 3  # refused sign-ins on as many ticks, and the next is tried only after REFUSED_WAIT: repeated ones can lock the mailbox
REFUSED_WAIT = timedelta(hours=1)
JUNK_HOLD_RUNS = 8  # sending has waited this many ticks in a row for the junk folder to be read through: a person is told
ADDRESS_SEARCH_LIMIT = 150  # addresses looked for in one command, those with a step waiting aside: a server may refuse a longer line
ADDRESS_LIMIT_ALERT = "mail:address_search_limit"
ADDRESS_REFUSED_ALERT = "mail:address_search_refused:"


class InboxInput(BaseModel):
    pass


class InboxOutput(BaseModel):
    mailbox: str = "not configured"
    fetched: int = 0  # emails read: most of the unrelated ones only as far as their header lines
    stored: int = 0
    skipped_own: int = 0
    skipped_unrelated: int = 0  # INBOX_SCOPE=related: not sales mail, so not stored
    read_to_end: bool = True  # False: more mail is waiting in a mailbox, so nothing may go out as if it had been read
    categories: dict[str, int] = Field(default_factory=dict)
    needs_review: int = 0
    proposals: int = 0  # A4: meeting-time proposals drafted for approval
    booked: int = 0  # meetings booked on their own inside an approved campaign


class InboxAgent(BaseAgent[InboxInput, InboxOutput]):
    name = "inbox_monitor"
    title = "Inbox Agent"
    responsibilities = [
        "Read new emails in the sales mailbox without changing it (read-only IMAP)",
        "Match replies to leads by thread and address; classify interest, objections, unsubscribes, bounces",
        "Stop sequences on any human reply; suppress unsubscribes and bounces; open opportunities when interested",
        "Draft suggested responses for approval and send uncertain or sensitive replies to review",
    ]
    input_model = InboxInput
    output_model = InboxOutput
    # a dropped connection or a server hiccup (socket, TLS or IMAP protocol error) is retried once in the run; a refused sign-in is not
    # (MailSignInRefused, which is none of these)
    retry_policy = RetryPolicy(max_attempts=2, retry_on=(*RetryPolicy().retry_on, OSError, imaplib.IMAP4.error))

    def __init__(self, ctx, mailbox: Mailbox | None = None, imap_factory=None):
        super().__init__(ctx)
        self.injected = mailbox is not None
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL  # for panel mailboxes (tests pass a fake server)
        self.mailbox = mailbox or (Mailbox(ctx.settings) if ctx.settings.imap_configured else None)
        self._refused: set[str] = set()  # the mailboxes that refused the sign-in in this run: a retry of the run does not ask them again
        self._signed_in_again = False
        self._tested: dict[str, datetime | None] = {}  # when Test connection last passed on each panel mailbox

    def _sources(self, session: Session) -> list[tuple[str, Mailbox, str, object]]:
        """(name, mailbox, state key, token) for each mailbox to read: the .env one (or one a test injects) and every
        enabled Super Admin panel mailbox with "read replies" on. Each keeps its own position."""
        out = []
        if self.mailbox is not None:
            out.append((f"{self.settings.imap_host} / {self.settings.imap_folder}", self.mailbox, "inbox.state",
                        self._oauth_token(session)))
        if self.injected:
            return out
        from aime import mail_accounts
        from aime.integrations import oauth

        for a in mail_accounts.readable(session):
            token = None
            if a.auth == "oauth":
                def token(account=a.id) -> str:
                    with self.ctx.http() as http:
                        return oauth.access_token(session, self.settings, http, "microsoft", "mail", account=account)
            out.append((f"{a.email} / {a.imap_folder}", Mailbox(mail_accounts.settings_for(self.settings, a),
                                                                  imap_factory=mail_accounts.imap_factory_for(a, self.imap_factory)),
                        f"inbox.state.mailbox{a.id}", token))
            self._tested[f"inbox.state.mailbox{a.id}"] = a.last_test_at if a.last_test_ok else None
        return out

    def _oauth_token(self, session: Session):
        """With MAIL_AUTH=oauth: a callable giving a fresh access token for the connected account."""
        if self.settings.mail_auth != "oauth":
            return None
        from aime.integrations import oauth

        def token() -> str:
            with self.ctx.http() as http:
                return oauth.access_token(session, self.settings, http, self.settings.mail_oauth_provider, "mail")
        return token

    def _meetings(self, session: Session, out) -> None:
        """A4: offer free slots for new meeting requests and raise reminders, when booking is switched on."""
        from aime import booking

        booking.reminders(session, self.settings)
        booking.sync_offered(session)
        ok, _why = booking.ready(session, self.settings)
        if not ok:
            return
        from aime.integrations import oauth
        from aime.integrations.calendar import CalendarError, calendar_for

        with self.ctx.http() as http:
            provider = self.settings.calendar_provider.lower()
            cal = calendar_for(self.settings, http,
                               lambda: oauth.access_token(session, self.settings, http, provider, "calendar"))
            try:
                out.proposals = booking.propose_pending(session, self.settings, cal, self.name)
                out.booked = booking.act_on_answers(session, self.settings, cal, self.name)
            except (CalendarError, oauth.OAuthError) as exc:
                session.add(Alert(level="warning", source=self.name, message=f"Meeting times not proposed: {exc}"))

    def _authentication(self, raw: bytes, p: ParsedEmail) -> str | None:
        """Who vouches for this email, as stored for the Calendly check and for "was the sender proven" (aime.mail_auth).
        AIME DKIM-verifies what it keeps ("aime.dkim; ..."); a server's own Authentication-Results line is kept only when
        that server is named in TRUSTED_AUTHSERV_IDS. A header written by the sender is never stored as trusted."""
        from aime.mail_auth import authserv_id, trusted_authserv_ids

        if p.auth_results and authserv_id(p.auth_results) in trusted_authserv_ids(self.settings):
            return p.auth_results
        from aime.dkim_verify import auth_results

        return auth_results(raw)

    def _store(self, session: Session, key: str, raw: bytes, own: set[str], out: InboxOutput, large: int | None = None,
               junk: bool = False, named: Collection[str] = ()) -> None:
        """`large`: the size of an email read only as far as its start. `junk`: it came from the junk folder, where only sales
        mail is kept and the rest leaves no trace (the folder would flood the list of skipped mail). `named`: the addresses we wrote
        to, when the server found one of them in this email's text."""
        p = parse_message(raw, own)
        # Our own mail is no reply, but a Calendly notice forwarded by hand from this mailbox is a notice.
        if p.from_email in own and not (CALENDLY_NOTICE.match(p.subject or "") and looks_like_calendly(p)):
            out.skipped_own += 1
            return
        if p.message_id and session.execute(select(InboundEmail.id).where(
                InboundEmail.message_id == p.message_id)).first():
            return
        # What decides may lie in the part of a large email that was not read: it is kept for a person, on what its headers say.
        if ((junk or self.settings.inbox_scope != "all") and not sales_related(session, p) and (junk or not (large and needs_the_body(session, p)))
                and not names_someone_we_wrote_to(p, named, large)):
            if not junk:
                self._note_unrelated(session, key, p, out)
            return
        authentication = self._authentication(raw, p)
        note = " ".join(x for x in (large_note(large) if large else None, p.note) if x)
        session.add(InboundEmail(
            mailbox_uid=key, message_id=p.message_id,
            in_reply_to=p.bounced_message_id if p.is_bounce and p.bounced_message_id else p.in_reply_to,
            references=p.references, from_email=p.from_email, from_name=p.from_name, to_email=p.to_email,
            subject=p.subject, received_at=p.received_at, body_text=p.body_text, new_text=new_text(p.body_text),
            auto_submitted=p.auto_submitted, bounced_addresses=p.bounced_addresses, status="new",
            auth_results=authentication, sender_verified=sender_signed(p.from_email, authentication),
            mail_kind=p.kind, mail_note=note[:400] or None, reply_to=p.reply_to, in_junk=junk or None))
        out.stored += 1

    @staticmethod
    def _note_unrelated(session: Session, key: str, p: ParsedEmail, out: InboxOutput) -> None:
        out.skipped_unrelated += 1  # read once (the mailbox position moves on): only who, what about and when is kept
        if not session.execute(select(SkippedMail.id).where(SkippedMail.message_id == p.message_id)).first():
            session.add(SkippedMail(mailbox_uid=key, message_id=p.message_id, from_email=p.from_email,
                                    subject=p.subject, received_at=p.received_at))

    def _pass_on_headers(self, session: Session, key: str, head: bytes, own: set[str], out: InboxOutput) -> None:
        """An email whose header lines show it is no sales mail: recorded as _store would have, without its body."""
        p = parse_headers(head, own)
        if p.from_email in own:
            out.skipped_own += 1
        elif not (p.message_id and session.execute(select(InboundEmail.id).where(InboundEmail.message_id == p.message_id)).first()):
            self._note_unrelated(session, key, p, out)

    def _wanted(self, session: Session, own: set[str], junk: bool = False):
        """Is this email's body needed, going by its header lines? When in doubt it is."""
        everything = not junk and self.settings.inbox_scope == "all"

        def wanted(head: bytes) -> bool:
            if everything:
                return True
            try:
                p = parse_headers(head, own)
            except Exception:  # noqa: BLE001 -- whatever a stranger's header lines do, the whole email decides
                return True
            if p.from_email in own:  # as in _store: only a notice forwarded from here is more than our own mail
                return bool(CALENDLY_NOTICE.match(p.subject or ""))
            return needs_the_body(session, p) or sales_related(session, p)
        return wanted

    def _keep_unreadable(self, session: Session, key: str, raw: bytes, own: set[str], exc: Exception,
                         out: InboxOutput, junk: bool = False) -> None:
        """An email that could not be read must neither stop the others nor disappear: it is kept without its text, so
        it is handled like an empty reply (the sequence of a known sender stops) and a person opens it in the mailbox."""
        sender = _sender_of_unreadable(raw)
        if sender in own:
            out.skipped_own += 1
            return
        stand_in = f"<unreadable-{hashlib.sha256(raw).hexdigest()[:32]}@aime.invalid>"
        if session.execute(select(InboundEmail.id).where(InboundEmail.message_id == stand_in)).first():
            return
        why = str(exc) if isinstance(exc, _StoppedTheJob) else type(exc).__name__
        self.log.warning("Email %s could not be read (%s)", key, why)
        session.add(InboundEmail(mailbox_uid=key, message_id=stand_in, from_email=sender, status="new", new_text="", in_junk=junk or None,
                                 subject=f"(could not be read: open it in the mailbox, {key})"[:300], body_text=""))
        session.add(Alert(level="warning", source=self.name,
                          message=f"An email from {sender} could not be read ({why}): please open it in "
                                  f"the {'junk folder' if junk else 'mailbox'} ({key}). It is on Replies."[:500]))
        out.stored += 1

    def _sign_in_waits(self, session: Session, box: Mailbox, state_key: str) -> bool:
        """The mailbox refused the sign-in on the last ticks, with this same password, and no Test connection has passed since: only
        one try an hour. Its mail stays unread, so the run still fails and nothing goes out on an older read."""
        if state_key in self._refused:
            return True
        seen = kv_get(session, f"{state_key}.refused")
        if not seen or seen.get("sign") != _password_sign(box) or int(seen.get("count", 0)) < REFUSED_TICKS:
            return False
        at = datetime.fromisoformat(seen["at"])
        tested = self._tested.get(state_key)
        return not (tested and tested > at) and utcnow() - at < REFUSED_WAIT

    def _note_refused(self, session: Session, name: str, box: Mailbox, state_key: str) -> None:
        self._refused.add(state_key)
        seen = kv_get(session, f"{state_key}.refused") or {}
        sign = _password_sign(box)
        kv_set(session, f"{state_key}.refused", {"count": int(seen.get("count", 0)) + 1 if seen.get("sign") == sign else 1, "sign": sign,
                                                 "at": utcnow().isoformat(timespec="seconds")})
        alert_once(session, "error", self.name, f"The sales mailbox {name} refused the sign-in, so new mail is not being read and approved emails "
                   "are waiting. Please change the app password on Mailboxes, then press Test connection. Until then AIME tries it once an "
                   "hour, because repeated refused sign-ins can lock the mailbox."[:500], self.settings, key=SIGN_IN_ALERT)
        session.commit()

    def _read_mailbox(self, session: Session, box: Mailbox, state_key: str, token, own: set[str], out: InboxOutput,
                      addresses: Collection[str] = ()) -> None:
        """The position is saved before each email, with a mark that it is being read. A run killed on one email (the
        scheduler's limit, a reboot, out of memory) therefore loses nothing, and after READ_TRIES such runs that email is
        set aside with an alert, so an opt-out behind it is still read."""
        before = kv_get(session, state_key)
        with box.connected(token) as conn:
            if kv_get(session, f"{state_key}.refused"):
                kv_set(session, f"{state_key}.refused", None)
                session.commit()
                self._signed_in_again = True
            raws, state = box.read_folder(conn, box.settings.imap_folder, before, self._wanted(session, own), quoting=FOOTER_WORDS,
                                          addresses=addresses)
            self._handle_inbox(session, state_key, before, raws, state, own, out, addresses)
            # The inbox is dealt with, so nothing that happens in the junk folder can cost it anything.
            self._read_junk(session, box, conn, state_key, own, out)

    def _handle_inbox(self, session: Session, state_key: str, before: dict | None, raws: list, state: dict, own: set[str],
                      out: InboxOutput, addresses: Collection[str] = ()) -> None:
        passed = state.pop("unrelated", None) or {}
        named = set(state.pop("named", None) or ())
        out.fetched += len(raws) + len(passed)
        out.read_to_end = bool(state.pop("complete", True)) and out.read_to_end
        connection_lost = state.pop("connection_lost", None)
        given_up_on = state.pop("skipped_headers", None) or {}
        same_numbering = bool(before) and before.get("uidvalidity") == state.get("uidvalidity")
        if before and not same_numbering:
            session.add(Alert(level="warning", source=self.name, message=f"The mailbox was moved or rebuilt ({state_key}): its "
                              "numbering changed, so AIME's reading position was reset and recent mail is being read again. "
                              "Please check the mailbox for older mail that was still unanswered."))
        self._name_what_was_given_up(session, before, state, given_up_on, state_key, own, out)
        position = int(before.get("last_uid", 0)) if same_numbering else 0
        reading = dict(state.pop("reading", None) or {})
        large = state.pop("large", None) or {}
        # In UID order: what is passed on its headers is written in the same step as the position that goes past it.
        items = [(int(uid), f"{state.get('uidvalidity')}:{uid}", head, True) for uid, head in passed.items()]
        items += [(int(key.rsplit(":", 1)[1]), key, raw, False) for key, raw in raws]
        for uid, key, raw, on_headers in sorted(items, key=lambda item: item[0]):
            if on_headers:
                self._pass_on_headers(session, key, raw, own, out)
                position = uid
                continue
            tries = int(reading.get(str(uid), 0))
            if tries >= READ_TRIES:
                self._keep_unreadable(session, key, raw, own, _StoppedTheJob(f"the job stopped on it {tries} times, so it was set aside"), out)
            else:
                # uid - 1: killed on the first email of a new position, the next run must not start again from UID 1
                position = max(position, uid - 1)
                kv_set(session, state_key, {**state, "last_uid": position, "reading": {str(uid): tries + 1}})
                session.commit()
                try:
                    self._store(session, key, raw, own, out, large=large.get(str(uid)), named=addresses if uid in named else ())
                    session.commit()
                except SQLAlchemyError:  # the database, not the email: the position stays, and it is read again next run
                    session.rollback()
                    self._undo_mark(session, state_key, {**state, "last_uid": position})
                    raise
                except Exception as exc:  # noqa: BLE001 -- whatever a stranger's email does, the good ones are still handled
                    session.rollback()
                    self._keep_unreadable(session, key, raw, own, exc, out)
                    session.commit()
            position = uid
            session.commit()
        kv_set(session, state_key, state)
        refused_search = f"{ADDRESS_REFUSED_ALERT}{state_key}"
        if state.get("no_address_search"):
            alert_once(session, "warning", self.name, f"The mail server will not search for the addresses AIME has written to ({state_key}), so an "
                       "opt-out sent from another address under a new subject can be read as not sales. Please look through \"Mail read as "
                       "not sales\" on Replies now and then. AIME asks the server again each day, and this closes when it answers.", self.settings, key=refused_search)
        resolve_alerts(session, (refused_search,), {refused_search} if state.get("no_address_search") else set())
        session.commit()
        if connection_lost is not None:  # what was read is handled; the run is a failed read, and tried again at once
            if int((state.get("dropped") or {}).get(str(connection_lost), 0)) >= READ_TRIES:
                alert_once(session, "warning", self.name, f"The connection to the mailbox drops each time AIME fetches the email "
                           f"with UID {connection_lost} ({state_key}): nothing behind it can be read until it is moved or the "
                           "connection holds. Please look for it in the mailbox.", self.settings)
                session.commit()
            raise ConnectionError(f"the connection to the mailbox dropped while fetching email UID {connection_lost}")

    def _name_what_was_given_up(self, session: Session, before: dict | None, state: dict, given_up_on: dict, state_key: str,
                                own: set[str], out: InboxOutput, junk: bool = False) -> None:
        for uid in state.get("skipped_uids") or []:  # the server would not give this one after several tries
            if uid in ((before or {}).get("skipped_uids") or []):
                continue
            if head := given_up_on.get(str(uid)):  # kept, so that a known sender's sequence stops and a person sees who it was
                self._keep_unreadable(session, f"{'junk:' if junk else ''}{state.get('uidvalidity')}:{uid}", head, own,
                                      _StoppedTheJob("it could not be fetched after several tries, and was skipped"), out, junk=junk)
            else:
                session.add(Alert(level="warning", source=self.name, message=f"An email (UID {uid}) could not be fetched "
                                  f"after several tries and was skipped ({state_key}): please look for it in the "
                                  f"{'junk folder' if junk else 'mailbox'}."))

    def _read_junk(self, session: Session, box: Mailbox, conn, state_key: str, own: set[str], out: InboxOutput) -> None:
        """A real answer filtered as spam must not be lost, so sales mail in the junk folder is kept. Only what proves itself (our own
        thread, or the sender's signature) is acted on at once: the rest may be forged, and waits for a person (reply_intel). The folder is
        read a little each run, with a position of its own, and sending waits while there is more to read (_handle_junk). Trouble with the
        folder itself raises one warning, fails nothing, and holds nothing: a false hold stops real work."""
        junk_key, alert = f"{state_key}.junk", f"mailbox_junk:{state_key}:"
        try:
            folder = box.junk_folder(conn)
            if folder is not None:
                self._handle_junk(session, box, conn, folder, junk_key, own, out)
            resolve_alerts(session, (alert,), set())
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- the inbox has been read: this must not fail the run
            session.rollback()
            self.log.warning("The junk folder (%s) could not be read (%s)", state_key, type(exc).__name__)
            try:
                alert_once(session, "warning", self.name, f"The junk folder of a sales mailbox could not be read ({type(exc).__name__}), so a "
                           "real answer filtered as spam may be waiting there. The inbox itself was read, and sending is not held. "
                           "Please look in the junk folder if this stays.", self.settings, key=alert)
                session.commit()
            except SQLAlchemyError:
                session.rollback()

    def _handle_junk(self, session: Session, box: Mailbox, conn, folder: str, junk_key: str, own: set[str], out: InboxOutput) -> None:
        before = kv_get(session, junk_key)
        raws, state = box.read_junk(conn, folder, before, self._wanted(session, own, junk=True))
        # An opt-out filtered as spam may lie behind what this run got through, so nothing goes out on this read. Said first: the storing
        # below can fail, and _read_junk swallows that. A dropped connection holds nothing, like trouble with the folder.
        short = not state.get("complete", True) and state.get("connection_lost") is None
        if short:
            out.read_to_end = False
        short_runs = int((before or {}).get("short_runs", 0)) + 1 if short else 0
        state["short_runs"] = short_runs
        state.pop("complete", None)
        state.pop("unrelated", None)  # what is no sales mail leaves no trace
        lost = state.pop("connection_lost", None)
        given_up_on = state.pop("skipped_headers", None) or {}
        large = state.pop("large", None) or {}
        position = int(before.get("last_uid", 0)) if before and before.get("uidvalidity") == state.get("uidvalidity") else 0
        reading = dict(state.pop("reading", None) or {})
        self._name_what_was_given_up(session, before, state, given_up_on, junk_key, own, out, junk=True)
        for key, raw in raws:
            uid = int(key.rsplit(":", 1)[1])
            tries = int(reading.get(str(uid), 0))
            if tries >= READ_TRIES:  # the header lines wanted it, so it is kept as in the inbox
                self._keep_unreadable(session, key, raw, own, _StoppedTheJob(f"the job stopped on it {tries} times, so it was set aside"), out, junk=True)
            else:
                position = max(position, uid - 1)
                kv_set(session, junk_key, {**state, "last_uid": position, "reading": {str(uid): tries + 1}})
                session.commit()
                try:
                    self._store(session, key, raw, own, out, large=large.get(str(uid)), junk=True)
                    session.commit()
                    out.fetched += 1
                except SQLAlchemyError:
                    session.rollback()
                    self._undo_mark(session, junk_key, {**state, "last_uid": position})
                    raise
                except Exception as exc:  # noqa: BLE001 -- spam may be built to break a parser: it never stops the rest
                    session.rollback()
                    self._keep_unreadable(session, key, raw, own, exc, out, junk=True)
                    session.commit()
            position = uid
        kv_set(session, junk_key, state)
        hold_alert = f"mailbox_junk_hold:{junk_key}"
        if short_runs >= JUNK_HOLD_RUNS:
            alert_once(session, "warning", self.name, f"Sending has waited {short_runs} runs in a row while AIME reads through a long junk folder ({junk_key}), "
                       "about 100 emails or 10 possible replies a run. It goes on by itself; a follow-up shows it as held until then.",
                       self.settings, key=hold_alert)
        resolve_alerts(session, (hold_alert,), {hold_alert} if short_runs >= JUNK_HOLD_RUNS else set())
        session.commit()
        if lost is not None:
            raise ConnectionError(f"the connection dropped while fetching junk email UID {lost}")

    @staticmethod
    def _undo_mark(session: Session, state_key: str, state: dict) -> None:
        """A database failure must not count against the email being read."""
        try:
            kv_set(session, state_key, state)
            session.commit()
        except SQLAlchemyError:  # still failing: the run stops here all the same
            session.rollback()

    def _watch_booking_notices(self, session: Session) -> None:
        """Booking links went out days ago and no Calendly notice was ever read: bookings are not being recorded."""
        count, oldest = booking_links_without_notice(session)
        active = set()
        if oldest is not None and utcnow() - oldest >= timedelta(days=BOOKING_NOTICE_DAYS):
            active.add(NOTICE_ALERT)
            alert_once(session, "warning", self.name, f"{count} booking link(s) sent, but no Calendly notice has ever "
                       "reached the monitored mailbox, so bookings are not recorded. Forward Calendly's notifications to "
                       "it (docs/SALES_SETUP.md section 6).", self.settings, key=NOTICE_ALERT)
        resolve_alerts(session, (NOTICE_ALERT,), active)

    def execute(self, inp: InboxInput, session: Session) -> InboxOutput:
        out = InboxOutput()
        from aime.models import MailAccount
        own = {a.lower() for a in (self.settings.smtp_from, self.settings.imap_credentials[0],
                                    self.settings.unsubscribe_email) if a}
        own |= {e.lower() for e in session.execute(select(MailAccount.email)).scalars()}  # our own mail is not a reply
        remember_sent_subjects(session)
        session.info["sent_companies"] = _sent_companies(session)
        # What an earlier run stored and could not finish (it died on the email behind them) is handled first.
        categories = process_pending(session, self.settings, self.name, llm=self.ctx.llm,
                                     conversation_llm=getattr(self.ctx, "conversation_llm", None))
        forget_old_skipped_mail(session)
        session.commit()  # no database lock is held while the mailbox is read over the network
        sources = self._sources(session) if self.settings.network_enabled else []
        if sources:
            out.mailbox = "; ".join(name for name, *_ in sources) + " (read-only)"
        addresses = self._addresses_to_look_for(session, own) if sources and self.settings.inbox_scope != "all" else []
        failed = []  # a mailbox that cannot be read fails the run, but only after the others' mail has been handled
        for name, box, state_key, token in sources:
            try:
                if self._sign_in_waits(session, box, state_key):
                    failed.append(MailSignInRefused(f"the mailbox {name} refused the sign-in: not asked again yet"))
                    continue
                self._read_mailbox(session, box, state_key, token, own, out, addresses)
            except MailSignInRefused as exc:
                session.rollback()
                self.log.warning("Mailbox %s refused the sign-in", name)
                self._note_refused(session, name, box, state_key)
                failed.append(exc)
            except Exception as exc:  # noqa: BLE001 -- raised again below
                session.rollback()
                self.log.warning("Mailbox %s could not be read (%s)", name, type(exc).__name__)
                failed.append(exc)
        for category, count in process_pending(session, self.settings, self.name, llm=self.ctx.llm,
                                                     conversation_llm=getattr(self.ctx, "conversation_llm", None)).items():
            categories[category] = categories.get(category, 0) + count
        out.categories = categories
        # An alert about an email in junk closes once a person has decided on it.
        resolve_alerts(session, (JUNK_ALERT,), {f"{JUNK_ALERT}{lead_id or 'none'}" for lead_id in session.execute(select(InboundEmail.lead_id).where(
            InboundEmail.in_junk.is_(True), InboundEmail.status == "needs_review")).scalars()})
        check_double_bookings(session, self.settings)  # after the whole batch: a reschedule's two notices are one change
        self._watch_booking_notices(session)
        self._meetings(session, out)
        out.needs_review = len(session.execute(select(InboundEmail.id).where(InboundEmail.status == "needs_review")).all())
        # A refusal only the reading saw ends when the reading signs in again, on every mailbox that refused: one still waiting holds the mail too.
        if self._signed_in_again and not any(kv_get(session, f"{key}.refused") for _, _, key, _ in sources):
            resolve_alerts(session, (SIGN_IN_ALERT,), set())
        if failed:
            session.commit()
            # What can be tried again goes first: the mailbox that refused is not asked again in this run.
            raise next((exc for exc in failed if not isinstance(exc, MailSignInRefused)), failed[0])
        return out

    def _addresses_to_look_for(self, session: Session, own: set[str]) -> list[str]:
        addresses, left_out = addresses_we_wrote_to(session)
        addresses = [a for a in addresses if a not in own]  # our own address is in most of our threads, and says nothing
        if left_out:
            alert_once(session, "warning", self.name, f"AIME looks through new mail for {len(addresses)} of the addresses it has written to and leaves out the "
                       f"oldest {left_out}, none with an email waiting: an opt-out naming one of those from another address may be read as not sales.",
                       self.settings, key=ADDRESS_LIMIT_ALERT)
        resolve_alerts(session, (ADDRESS_LIMIT_ALERT,), {ADDRESS_LIMIT_ALERT} if left_out else set())
        session.commit()  # no database lock is held while the mailbox is read over the network
        return addresses


REPLY_SUBJECT = re.compile(rf"\s*(?:{REPLY_WORDS})\s*[:：]", re.I)
FOOTER_WORDS = "to stop receiving emails from us"  # the line every email of ours ends with
OUR_FOOTER_QUOTED = re.compile(FOOTER_WORDS, re.I)
STEP_WAITING = ("pending", "awaiting_approval", "approved", "sending")
# As a one-word company name these say nothing, in a subject line: a name of only such words is not looked for (with GENERIC_TOKENS).
COMMON_NAME_WORDS = frozenset("""
    about account action advance advantage agency alert alliance alpha american analytics answer apex apple bank best better black blue bright
    business call capital care career careers catalyst change chance click cloud commerce connect consulting contract creative data deal demo
    design digital direct discover elevate energy enterprise enterprises estate event events express family federal finance financial first focus
    forward free future general global gold good great green growth health hello help hire hiring holdings home homes horizon ideas ignite impact
    industries info insight insights insurance international invoice job jobs law leader leading legal link logistics marketing media meeting
    mobile momentum national net network networks new news newsletter next notice offer office one online open order our partner partners payment
    people pioneer plus premier prime pro project proposal quality question quick real receipt red report request review sale sales security
    service services simple smart software solution solutions source spark staff start studio studios summit support systems target team tech
    technology technologies thank thanks today top total trial true trust united update urgent value vision web webinar welcome white with work
    world you your
    """.split())


def _password_sign(box: Mailbox) -> str:
    """Enough to notice that the sign-in changed, far too little to say anything about the password."""
    return hashlib.sha256("\0".join(str(x) for x in box.settings.imap_credentials).encode()).hexdigest()[:4]


def addresses_we_wrote_to(session: Session) -> tuple[list[str], int]:
    """The addresses of sent emails, for the search of new mail, and how many the limit left out: never one whose lead has an email waiting,
    then the most recent."""
    waiting = set(session.execute(select(FollowUp.lead_id).where(FollowUp.channel == "email", FollowUp.status.in_(STEP_WAITING))).scalars())
    must, rest = {}, {}
    for to, lead_id in session.execute(select(FollowUp.to_email, FollowUp.lead_id).where(
            FollowUp.status == "sent", FollowUp.channel == "email", FollowUp.to_email.is_not(None)).order_by(FollowUp.sent_at.desc(), FollowUp.id.desc())).all():
        (must if lead_id in waiting else rest)[to.strip().lower()] = True
    others = [a for a in rest if a not in must]
    room = max(0, ADDRESS_SEARCH_LIMIT - len(must))
    return [*must, *others[:room]], max(0, len(others) - room)


def names_someone_we_wrote_to(p: ParsedEmail, named: Collection[str], large: int | None = None) -> bool:
    """The server found an address we wrote to in this email's text. It looks for any part of a longer address too, so here only the
    whole address counts. What it found beyond the part we keep cannot be checked, so a person reads it. A newsletter or a robot naming
    someone is no one asking for anything. This only keeps the email: it suppresses and ties nothing (reply_intel decides what it holds)."""
    if not named or p.kind == "list" or p.auto_submitted or NO_REPLY.match(p.from_email):
        return False
    body = p.body_text or ""
    if large or len(body) >= BODY_LIMIT:
        return True
    body = body.lower()
    return any(a in body and re.search(rf"(?<![\w.+%-]){re.escape(a)}(?![\w-]|\.\w)", body) for a in named)


def _plain_words(text: str) -> str:
    """Spelled as normalize_name spells a company."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("&", " and ")).split())


def _sent_companies(session: Session) -> list[str]:
    """The names of the companies a sent outreach email went to, spelled for a subject line. A name of only common or generic words is left out."""
    names = set()
    for name in session.execute(select(Company.name).join(Lead, Lead.company_id == Company.id).join(FollowUp, FollowUp.lead_id == Lead.id).where(
            FollowUp.status == "sent", FollowUp.channel == "email", FollowUp.kind == "outreach").distinct()).scalars():
        plain = normalize_name(name or "")
        if any(len(w) >= 3 and not w.isdigit() and w not in GENERIC_TOKENS and w not in COMMON_NAME_WORDS for w in plain.split()):
            names.add(plain)
    return sorted(names)


def names_company_we_emailed(session: Session, subject: str | None) -> bool:
    """A prospect writing from a private address under a subject of their own. Whole words only. Such mail reaches a person like any other
    unmatched mail: this ties it to no lead and changes no gate."""
    names = session.info["sent_companies"] if "sent_companies" in session.info else _sent_companies(session)
    words = f" {_plain_words(subject or '')} "
    return any(f" {name} " in words for name in names)


def looks_like_calendly(p: ParsedEmail) -> bool:
    """A Calendly booking notice, sent by Calendly or forwarded (its subject and a calendly.com mention)."""
    return bool((calendly_sender(p.from_email) or "calendly.com" in (p.body_text or "").lower())
                and (CALENDLY_SUBJECT.search(p.subject or "") or CALENDLY_NOTICE.match(p.subject or "")))


class _StoppedTheJob(Exception):
    """Runs were killed while reading this email, again and again."""


def _sender_of_unreadable(raw: bytes) -> str:
    """The sender of an email that could not be parsed, read from the raw header lines. Only the start of the email and of
    the line is looked at: no pattern runs over a stranger's whole line."""
    found = re.search(rb"(?im)^from:([^\r\n]{0,%d})" % HEADER_LIMIT, raw[:256 * 1024])
    address = re.search(rb"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", found.group(1)) if found else None
    return address.group(0).decode("ascii", "replace").lower() if address else NO_ADDRESS


def needs_the_body(session: Session, p: ParsedEmail) -> bool:
    """What sales_related decides from an email's body or parts, and so cannot decide from its header lines: calendly.com in a notice's
    text and what kind of mail a sender at a lead's sub-domain writes; and for a large email read only in part, a quoted footer under
    a reply's subject. (A footer quoted under any other subject the server finds for us: Mailbox.read_folder, `quoting`.)"""
    subject = p.subject or ""
    return bool(REPLY_SUBJECT.match(subject) or CALENDLY_SUBJECT.search(subject) or CALENDLY_NOTICE.match(subject)
                or subdomain_leads(session, p.from_email))


def sales_related(session: Session, p: ParsedEmail) -> bool:
    """Is this email part of the sales work? A reply in one of our threads, a bounce, a Calendly booking notice (also
    forwarded), mail from a known contact, a lead's company domain or someone who answered in our thread before, or an
    answer that carries the subject of an email we sent or quotes our footer (helpdesks and personal addresses write
    without our thread headers), or what its subject calls a failed delivery, from whichever sender: a person reads it, and what it
    does is decided as for any other mail; or a subject that names a company we emailed. Everything else in a shared mailbox, Calendly's
    own newsletters included, is left alone."""
    if p.is_bounce or p.bounced_addresses or BOUNCE_SUBJECT.search(p.subject or ""):
        return True
    if looks_like_calendly(p):
        return True
    ids = {i.strip() for i in [p.in_reply_to or "", *(p.references or "").split()] if i.strip()}
    if ids and session.execute(select(FollowUp.id).where(FollowUp.message_id.in_(ids))).first():
        return True
    if session.execute(select(Contact.id).where(func.lower(Contact.email) == p.from_email)).first():
        return True
    if company_leads(session, p.from_email) or remembered_lead_id(session, p.from_email):
        return True
    if sent_with_our_subject(session, p.subject) or UNSUBSCRIBE_SUBJECT.match(p.subject or ""):  # our unsubscribe link, from any alias
        return True
    if (p.kind != "list" and not p.auto_submitted and not NO_REPLY.match(p.from_email)  # a person at a sub-domain, not its robots
            and subdomain_leads(session, p.from_email)):
        return True
    if names_company_we_emailed(session, p.subject):
        return True
    return bool(OUR_FOOTER_QUOTED.search(p.body_text or ""))
