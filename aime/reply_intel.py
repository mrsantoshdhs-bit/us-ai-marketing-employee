"""Reply intelligence: classify replies, match them to leads, update the CRM, suggest responses.

Categories (first match wins, safety first): bounce, calendly, unsubscribe (including complaints),
out_of_office, then the human answers interested / not_interested / objection / referral / question,
else other. Conflicting signals or a complaint lower the confidence and send the reply to review.

Interest includes meeting requests in everyday wording ("can we talk Thursday?", "let's arrange a call",
"I'd like a demo") and common misspellings ("sound interesting", "intrested"). A negation in front of it
("don't schedule a call") still sends the reply to review.

Opt-outs are also looked for in the sender's own words below the quote and past the stored text. One found there
next to interest or a question is not acted on: a person reads it. A legal word or "spam" with no accusation ("is it
GDPR compliant?", "it was in my spam folder") is not a complaint: nothing is suppressed, and a person checks the reply.

An opt-out only in a company's own footer ("click here to unsubscribe"), beside something else the writer says, is not the
writer's: the sequence stops, nothing is suppressed, and a person reads it. With nothing else said it counts: suppressed, and a
person checks. A plain opt-out is honoured at once, whatever else the email says. Opt-out words that are only a legal notice's "delete it
from your system", about their own business ("we need to stop sending invoices by hand") or with a date ("don't email me until April")
suppress nothing: the sequence stops, a person reads it, and the date gets a reminder.

Every human reply stops the automated sequence, but anyone can write under a subject they guessed: someone outside the
lead's company with no thread only holds that lead's emails until a person decides. Unsubscribes suppress the address. A
failure report suppresses only the recipient of the email whose Message-ID it quotes. If it quotes ours and names someone
else, or a mail system names an address we wrote to and quotes nothing, the sequence stops and a person decides. A report
naming anyone else changes nothing and goes to a person.
Interested replies open (or advance) an opportunity and notify the Sales Manager. Suggested responses
are drafts that wait for approval like every other email. A simulated reply (`aime simulate-reply`) stops the sequence and
is labelled SIMULATED wherever it appears, but opens no deal and writes no draft unless a person reclassifies it.

When the rules can only say "question" or "other", the local AI model (if one is running) gives a second
opinion. It never acts on its own: a reply it reads as interest stays in review, with the suggestion in its
reasons and a "possible interest" alert, and the Sales Manager decides (Replies page: reclassify).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aime.crm_ops import BOUNCED_REASON, OPEN_LEAD_STATUSES, _entries_covering, WROTE_AGAIN, came_with_the_merges, is_suppressed, later_reply_waiting, lead_with_the_deals, log_activity, primary_lead, suppress
from aime.db import kv_get, kv_set
from aime.dedupe import company_to_address
from aime.inbox import unprefixed_quote, AUTO_SUBJECT, BOUNCE_SENDERS, LARGE_MARK, below_header_copy, between_header_copies, is_address, own_text, rest_of_reply
from aime.llm.base import LLMError
from aime.mail_auth import _within, trusted_forwarders
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity, SkippedMail
from aime.notify import notify_event
from aime.integrations.email_smtp import sender_line
from aime.outreach import CHECK_BACK_TASK, ENDS_CHECK_BACK, TOPICS, cancel_queued, close_check_back_tasks, company_opted_out, stop_sequence
from aime.pipeline import BOOK_ASKING_TIMES, WRITE_THE_ANSWER
from aime.stages import CLOSED, move, opened
from aime.timeutil import utcnow

log = logging.getLogger("aime.inbox")

_OBJECT = (r"(me|us|my (e-?mail( address)?|address|name|details|contact details|info|information)|"
           r"our (e-?mails?( address(es)?)?|address(es)?|details|company|name)|my company)")
_LIST = r"((mailing|e-?mail|contact|marketing|distribution|sales|prospect|outreach) )?(list|lists|database)"
# Wider than the opt-out wording on purpose: only used to stop treating an email as a plain automatic reply.
REMOVAL_WORDS = re.compile(r"\b(remov(e|ed|al)|unsubscrib\w*|delet(e|ed)|stop|opt(ed|ing)?[- ]?out|"
                           r"(do not|don'?t|never) (e-?mail|contact|message|send|write)|take (me|us|my|our)|"
                           r"(off|from) (your|the|this) \w*\s?list)\b", re.I)
# "How much longer do I have to put up with this?" asks for no price.
_HOW_MUCH = r"how much(?! longer| more (of|time|do|can|must)| (do|must|can|should) i\b)"
# Having no money for it, in everyday wording.
_NO_BUDGET = (r"\b(no|zero|without( a| any)?|out of|(do not|don'?t|doesn'?t|did not|didn'?t) (have|get)( a| any| the)?|have not got( a| any| the)?)\s+budget\b|"
              r"\bbudget (is|was|has been) (\w+ )?(gone|tight|frozen|cut|spent|used up|exhausted|allocated|limited|locked|closed)\b|\bbudget freeze\b|"
              r"\b(over|above|beyond) (our |the )?budget\b(?![^.!?\n]*\?)")  # "over budget, unless you can do 5k?" still asks
PATTERNS: list[tuple[str, re.Pattern]] = [
    # Opt-outs in everyday wording and with typos ("unsubcribe", "remove my email from your list", "no more emails").
    # "drop me a line", "take us through it" and "one from the list you sent" are not opt-outs.
    ("unsubscribe", re.compile(
        r"\b(un-?su[bs]{1,2}c?r?i?b?e?d?|unsubscirbe|rmeove me|(?<!n't )(?<!not )(?:dnc|remove em|lose this address)(?=\W*$)|(do not|don'?t|dont) emial|pleas stop|no more emial|un su[bs]{1,2}cri?b\w*|un?s?c?u?s?b?s?cri?be?d?|"
        rf"(remove|remoev|remvoe|delete|erase|unlist) {_OBJECT}|"
        rf"drop {_OBJECT}(?! (a|an|the|some|any|your|our|my|one|in|by|at|to)\b)|"
        rf"(take|get) {_OBJECT} (off|out( of| from)?)(?! (topic|guard|to|for|on|and)\b)|"
        rf"{_OBJECT} (off|from|out of) (your|the|this|all|any) ({_LIST}|crm|system|records?)|"
        rf"(remov(e|ed|ing)|delet(e|ed|ing)|eras(e|ed|ing)|drop(ped|ping)?|strike|struck|cross(ed)?|scrub(bed)?|purg(e|ed)|"
        rf"exclud(e|ed))\b([^.?!\n]|(?<=\w)\.(?=\w)){{0,40}}\b(off|from|out of) (your|the|this|all|any) {_LIST}(?! (of|you|we|that)\b)|"
        rf"from (your|this) {_LIST}\W+(please )?(remov|delet|drop|unsub)\w*|"
        rf"(off|out of) (your|the|this) {_LIST}(?! of\b)|"
        r"from (your|the|this) (mailing|e-?mail|marketing|distribution) list|"
        r"(stop|quit|cease) (e-?maili?ng|contacti?ng|sending|messaging|mailing|writing to|spamming|bothering|pestering)|"
        r"(stop|quit|cease) (all |the |these |those |your )?(e-?mails?|messages?|spam)|(stop|quit|cease) (these|writing)\b|stop for good|"
        r"discontinue (all |the |these |those |your )?(e-?mails?|messages?|mail(ings?)?)(?! (our|their)\b(?! (team|staff|office|people|company)\b))(?![^.!?\n]*\b(statements?|invoices?|vendors?|can your|can you (replace|help|build|automate|do))\b)|discontinue (e-?mailing|contacting|messaging|mailing|sending (me|us)\b)(?! (our|their)\b(?! (team|staff|office|people|company)\b))(?![^.!?\n]*\b(statements?|invoices?|vendors?|can your|can you (replace|help|build|automate|do))\b)|"
        r"no (more|further) (e-?mails?|messages?|contact|mails?)|no more of (these|this|your)\b|"
        r"(do not|don'?t|never)( ever)? (e-?mail|contact|message|write|reach out|send)( to)?( (me|us))? (again|any ?more)|"
        r"please (remove|delete|go away|stop)(?=\W*($|[.!]))|(i|we) need you to stop(?=\W*($|[.!]))|"
        rf"(remove|delete|take)( off| from) {_LIST}|"
        r"(remove|delete|erase) (\S+|(this|my|that|our) (e-?mail )?address) from (your|all) (records?|systems?)|(remove|delete|erase) (this|my|our) (e-?mail )?address(?=\W*($|[.!]))|"
        r"(do not|don'?t|never)( ever)? (e-?mail|contact|message|write to|reach out to) (me|us)|do not (email|contact)(?! (the|this|that|a|an|our|my)\b)|"
        r"(do not|don'?t|never)( ever)? send(ing)? (me |us )?(any |anymore )?((more|further|other) )?(e-?mails?|messages?|mail|correspondence)|"
        # "Don't call or email me again", but "don't forget to call or email me again" and "don't hesitate to ..." invite them.
        r"(do not|don'?t|never)( ever| you dare| try to| even try to| bother( to)?)? (call|phone|ring|text|bother|pester|visit|e-?mail|contact|message|write to|reply( to (this|that|it|me|us))?)(ing)?( or (call|phone|ring|text)(ing)?)?( (me|us))?( again)? or (e-?mail|contact|message|write to)(ing)? (me|us) (again|any ?more)|"
        # Only when nothing follows: "don't send me more info" and "don't send more details, just call me" stop nothing.
        r"(do not|don'?t|never) send (me |us )?(any |anything )?(more|further|else)( of (these|this|them|your \w+))?(?=\W*((please|pls|thanks?|thank you|regards|bye|goodbye|best)\W*)*((best|regards)\W+\w+\W*)?$|\W*(i mean it|last warning|(i'?m|we'?re|i am|we are) not (interested|buying))\W*$)|"
        r"(not interested|no thanks|no, thank you)\W+(please |pls |just )?stop(?=\W*($|[.!]))|"
        # Each of these a notice's own words used to catch by accident; they count by their own words now.
        r"go away(?=\W*($|[.!,]))|no contact( please)?(?=\W*($|[.!]))|stop sendin\b|take this address off|(do not|don'?t) want any(?=\W*$)|"
        r"(add|put) (me|us) (on|to) your (do[- ]not[- ](e-?mail|contact|call)|no[- ]contact|suppression|unsubscribe|opt[- ]?out) list|"
        r"(do not|don'?t|never)( ever)? (contact|e-?mail|message|write to) (this|our) (company|firm|organi[sz]ation|business|team|staff|office)\b(?! (yet(?! again)|until|directly|before)\b)|"
        r"where did you get (my|this) (e-?mail( address)?|address)\W+(please )?(delete|remove)\b|"
        rf"(stp|stopp|sotp|stpo|stop) (e-?mailing|emialing|emailng)|take (me|us) of (your|the|this) {_LIST}|(do not|don'?t) (contat|contct|cotact) (me|us)|"
        r"forget (this|my|our) (e-?mail )?address|never (want to |wish to |to )hear from (you|your|anyone)\b(?![^.!?\n]*\?)|never hear from (you|your)\b[^.?!]{0,30}\bagain(?![^.!?\n]*\?)|"
        r"(do not|don'?t|no longer)( \w+)? (wish|want|like|need) to (receive|get)( \w+){0,3} "
        r"(e-?mails?|messages?|mail|communications?|newsletters?)|"
        r"(do not|don'?t|no longer)( \w+)? (wish|want|like|need) to (receive|get) (any )?(more|further) of (these|this|those|them)(?=\W*($|[.!,]))|"
        r"(do not|don'?t|no longer)( \w+)? (wish|want|like|need) to hear (back )?from (you|your)|"
        r"(do not|don'?t|no longer)( \w+)? (wish|want) to be (contacted|e-?mailed|on your)|"
        r"(do not|don'?t|no longer)( \w+)? (want|need) (any |your |these |those |more |further ){1,3}(e-?mails|messages)|"
        r"(do not|don'?t|never)( ever)? (e-?mail|contact|message|write to) (this (address|e-?mail|mailbox|account)|anyone)|"
        r"(stop|cease|end|halt|quit|discontinue) (all |any |further |these |your )*(communications?|contact|correspondence|outreach|solicitations?)|"
        r"refrain from (contacting|e-?mailing|messaging|writing)|stop following up|"
        r"(do not|don'?t) follow[- ]?up(?! (on|with|until|till|before|after|next|this|in|today|tomorrow|monday|tuesday|wednesday|thursday|friday)\b)|"
        r"(do not|don'?t|never) follow[- ]?up with (me|us) (again|any ?more)|"
        r"cancel (my|our|this) (subscription|membership)|lose (my|our) (e-?mail|address|number|details|contact)|"
        r"(remove|take) (me|us) (from|off) (all )?(future )?(correspondence|mailings?|communications?|the distribution)|"
        r"take (me|us) off the distribution|(me|us) out of your system|"
        r"(kindly |please )?desist|(do not|don'?t) solicit|no solicit(ation|ing)|"
        r"leave (me|us) out of (your|these|those|all) (\w+ )?(e-?mails?|mailings?|messages?|lists?)|"
        r"opt (me |us )?out|opt[- ]?out|leave (me|us) alone)\b", re.I)),
    # The three weak ones only when they end their sentence: "We're good to go" and "No need for a long call" say no such thing.
    ("not_interested", re.compile(r"\b(not interested|no thanks|no,? thank you|not a (good )?fit|"
                                  r"not (looking|in the market)|pass on this|not for us|"
                                  r"(we'?re good|we'?re all set|no need)(?=\W*(thanks?\b|thank you\b|thx\b|$)|\s*[.!;]))\b", re.I)),
    ("interested", re.compile(r"\b(interested|sounds? (good|great|interesting|in(te|t|tr)[a-z]{0,3}st(ed|ing))|"
                              r"in(te|tr)[a-z]{0,3}st(ed|ing)|let'?s (talk|chat|connect|set up)|"
                              r"happy to (chat|talk|connect|meet)|book a (time|call|meeting)|schedule a (call|meeting)|"
                              r"set up a (call|meeting)|tell me more|send (me )?(more|some) (info|details)|send (me|us) a time|(?<!is this )(?<!is it )(?<!would it be )(?<!would this be )worth a (chat|conversation|call)(?![^.!?,\n]*\?)|"
                              rf"what (does it|would it) cost|{_HOW_MUCH}|pricing|availab(le|ility)|free (on|next)|"
                              r"(monday|tuesday|wednesday|thursday|friday) (works|at|morning|afternoon))\b", re.I)),
    ("objection", re.compile(rf"{_NO_BUDGET}|" r"\b(too expensive|budget (is|was)|not (a priority|right now|now)|maybe later|"
                             r"next (quarter|year)|bad timing|already (work|working|use|using|have)|in-house|"
                             r"circle back|check back|(we'?ll|we will) pass(?! (this|it|that|your|along|on|to|the|these|those|them|him|her|you)\b))\b", re.I)),
    ("referral", re.compile(r"\b(you should (talk|speak|reach out) to|reach out to|the right person|"
                            r"(cc|cc'?ing|copying|looping in)\b|forward(ed|ing)? (this|your)|contact our)", re.I)),
]
# Wordings that are a no only when they end the message and it speaks of no talk: "No need to write again." is one, "No need to write again, I'll call you Tuesday" is not.
# A list of what may follow cannot cover how people ask to talk, so anything else behind them goes to a person, with the sequence stopped and nothing suppressed.
# Everything else in PATTERNS reads the same whatever else the message says.
SOFT_OPT_OUT = re.compile(r"\b((stop|quit|cease) with (all |the |these |those |your )?(e-?mails?|messages?|spam)|no need to (write|e-?mail|contact|message)( to)?( (me|us))?( again| any ?more)|"
                          r"enough (e-?mails?|of (these|this|your))\b)", re.I)
SOFT_ENDS_IT = re.compile(r"\W*((please|pls|thanks?|thank you|regards|bye|goodbye|best|already|now|ok|okay|e-?mails?|messages?|mail|spam|from you|and get off my back|(do not|don'?t|never) call( me| us)?)\W*)*((best|regards|thanks)\W+\w+\W*)?", re.I)
SPEAKS_OF_A_TALK = re.compile(r"(?<!don't )(?<!do not )(?<!never )\b(call\w*|talk\w*|meet\w*|chat\w*|speak\w*|phone\w*|ring|book\w*|signed up|interested)\b", re.I)


# What may follow a soft wording and only makes it surer: a lawyer or a regulator as the whole of the rest, or that they are not interested.
SOFT_REFUSAL_TAIL = re.compile(r"\W*(e-?mails?\W+|messages?\W+)?(((and|or) )?((i'?m|i am|we'?re|we are|just) )*not interested( in (a |any )?(call|meeting|chat|demo|talk\w*))?|"
                               r"((and|or) )?((i'?m|i am|we'?re|we are|i'?ll|i will|we'?ll|we will) )?(call(ing)?|talk(ing)?( to)?|speak(ing)?( to| with)?|contact(ing)?|go(ing)? to) "
                               r"(my|our|the) (lawyers?|attorneys?|counsel|legal( department| team)?|ftc|fcc|ag|attorney general|police|authorities)|"
                               r"call (me|us) again and (i|we)('ll| will)? sue)\W*((thanks?|thank you|regards)\W*)?", re.I)
# They stop the email but ask for a talk: the opt-out stands and a person reads it the same day.
TALK_INSTEAD = re.compile(r"\b(?:(?:stop|quit|cease)(?: sending)?(?: the| these| your)? e-?mail(?:s|ing)?(?: tag)?(?: me)?|(?:do not|don'?t) (?:send more e-?mails|e-?mail me))(?:,| and)(?: just| please| give me a)* "
                          r"(?:call me|call|just talk|talk|pick up the phone|get on a call)\b(?![^.!?\n]*\b(never|off|again|someone|sue|lawyer|attorney|manager)\b)", re.I)
NAMES_WHERE_TO_GO = re.compile(r"\b(use|moved to|now at|write to|e-?mail) [\w.+-]+@[\w-]+\.|\b(contact|try|talk to|speak (to|with)) (?!my\b|our\b|the\b|a\b|an\b)[A-Z]\w+\b")


def _opt_out_words(said: str) -> re.Match | None:
    """The opt-out wording in these words, with the guard above on the soft ones."""
    if hard := dict(PATTERNS)["unsubscribe"].search(said):
        return hard
    soft = SOFT_OPT_OUT.search(said)
    if not soft:
        return None
    behind = said[soft.end():]
    return soft if SOFT_REFUSAL_TAIL.fullmatch(behind) or (SOFT_ENDS_IT.fullmatch(behind) and not SPEAKS_OF_A_TALK.search(said)) else None


# "I'm not the right person" points nowhere: nobody is referred, and a person reads it like "Wrong person, sorry".
NOT_THE_RIGHT_PERSON = re.compile(r"(\bnot|n't|\bnever|\bno longer)( (?!but\b|however\b|though\b)[\w']+){0,6} the right person\b", re.I)
# "I won't be forwarding this", "I never forwarded your email": nothing was passed on, and nobody is referred.
NOT_PASSED_ON = re.compile(r"(\bnot|\bno\b|n't|\bnever|\bno longer)( (?!but\b|however\b|though\b)[\w']+){0,4} (forward|pass|shar)\w*\b( on)?( (this|your|it|the)\b)?", re.I)
# Asking when we are free is a question, not interest, unless it is for a call they want ("your availability for a call").
OUR_AVAILABILITY = re.compile(r"\b((what|how|when)\b[^.?!]{0,30}\byour( team'?s)? availability|(do|does) (you|your team) have (any )?availability)\b"
                              r"(?![^.?!]*\b(call|meeting|chat|demo)\b)[^.?!]*\?", re.I)
# Opt-outs in the languages our prospects write in (Hindi in Latin script). Read by rules, not understood, and it suppresses for
# good: so only wording whose meaning is plain, naming what is to stop. Anything else ("dar de baja un proveedor", "keine E-Mail
# bekommen", "mail nahi mila") is read as an unclear reply: the sequence stops, and a person reads it.
_ES_LIST = r"de (su|sus|la|esta|vuestra|tu) (lista|listas|base|bases|correo|correos|env[ií]os|mailing)"
_ES_MAIL = r"(correos?|e-?mails?|mensajes?|publicidad|comunicaciones)"
_DE_MAIL = r"(e-?mails?|mails?|nachrichten|newsletter|werbung)"
_FR_LIST = r"(de|du) (votre|vos|la|cette|ce|votre) (liste|listes|base|fichier|mailing)"
_HI_MAIL = r"(e-?mails?|mails?|msgs?|messages?)"
_PT_MAIL = r"(e-?mails?|mensagens|correspond[eê]ncias?)"
ELSEWHERE_OPT_OUT = re.compile(
    rf"ईमेल (?:मत|न) भेज|सूची से हटा|बंद करो|\b(hata do|elim[ií]n(en|e|a) mi correo|stop merci|elim[ií]n(en|e|a)me|elim[ií]nenos|eliminarme {_ES_LIST}|qu[ií]t(en|e|a)me {_ES_LIST}|s[aá]qu(en|e|a)me {_ES_LIST}|"
    rf"b[oó]rr(en|e|a)me {_ES_LIST}|darme de baja|darnos de baja|d[eé]n(me)? de baja|"
    rf"no me (env[ií]en|mand(en|e)|escriban|contacten) m[aá]s|no me (env[ií]en|mand(en|e)) {_ES_MAIL}|"
    rf"dej(en|e|a) de (escribir|contactar)(me|nos)|dej(en|e|a) de (enviar|mandar)(me|nos) (\w+ )?{_ES_MAIL}|(?<!si )no me (escriban|contacten?)|no quiero recibir (m[aá]s )?((sus|tus|estos|esos|vuestros) )?{_ES_MAIL}|"
    rf"aus (ihrem|eurem|dem|ihrer|der) (verteiler|liste|e-?mail-?verteiler)|tragen sie (mich|uns)( bitte)? aus|nehmen sie (mich|uns)( bitte)? (von|aus) (ihrer|ihrem|der|dem) (\w*liste|\w*verteiler)|"
    rf"(mich|uns)\b[^.?!]{{0,40}}\baustragen|keine weiteren? {_DE_MAIL}(?! \w* ?(erhalten|bekommen))|keine {_DE_MAIL} mehr|bitte keine {_DE_MAIL}|"
    rf"(vom newsletter|von (ihrem|dem|der|ihrer) \w+|aus (ihrem|dem) \w+) abmelden|(bitte|m[oö]chte|will)\b[^.?!]{{0,40}}\babbestellen|"
    r"entfernen sie (mich|uns)|(mich|uns)\b[^.?!]{0,30}\bnicht mehr (kontaktieren|anschreiben|schreiben|anrufen)|nicht mehr (kontaktieren|anschreiben)|l[oö]schen sie (mich|meine)|"
    rf"me (retirer|supprimer) {_FR_LIST}|me d[eé]sabonner|me d[eé]sinscrire|retirez[- ]moi|d[eé]sabonnez[- ]moi|d[eé]sinscrivez[- ]moi|supprimez[- ]moi|"
    r"ne m'envoyez plus|ne plus me contacter|ne (plus recevoir|souhaite plus recevoir|veux plus recevoir) (vos|ces|de|d'|des|votre)|ne me contactez plus|"
    r"arr[eê]tez de (m'|nous )envoyer (\w+ )?(e-?mails?|mails?|messages?|courriels?)|arr[eê]tez de (me contacter|nous contacter|m'[eé]crire|nous [eé]crire)|"
    r"non (contattatemi|contattateci|scrivetemi|scriveteci) pi[uù]|"
    rf"parem de (me|nos) (enviar|mandar) (\w+ )?{_PT_MAIL}|n[aã]o me (enviem|mandem) mais (\w+ )?{_PT_MAIL}|"
    r"verwijder (mij|me|ons) (van|uit) (uw|jullie|je|de|deze) (\w*lijst|bestand|database)|stuur (mij|me|ons) geen (\w+ )?(e-?mails?|mails?|berichten)|schrijf (mij|me|ons) uit|"
    r"avregistrera (mig|oss)|avmeld (meg|oss)|afmeld (mig|os)|"
    r"usu[nń]\w* (mnie|nas) z (\w+ )?(listy|bazy)|wypisz(cie)? (mnie|nas)|"
    r"удалите (меня|нас) из (\w+ )?(списка|рассылки|базы)|не пишите (мне|нам) больше|больше не пишите (мне|нам)|отпишите (меня|нас)|"
    r"لا ترسل(وا)? (لي|لنا) (\w+ )?رسائل|"
    rf"{_HI_MAIL}( \w+)? (mat|na|nahi|nahin) bhej(o|iye|ein|en|e|na)|(mujhe|humein|hume) (mat|na) bhej(o|iye|ein|en|e|na)|"
    rf"{_HI_MAIL} band kar(o|do|iye|en|dein)?|bhejna band|(list|suchi) se hata\w*)\b|"
    # Written without spaces, so with no word edge to look for.
    r"メール配信を停止|配信を停止して|メールを送らないで|不要再(给|給|向)(我们|我們|我)(发|發|寄)(送)?(邮件|郵件|电邮|電郵)", re.I)
# The same, only when it is all the message says: on its own it can mean nothing else ("Baja", but not "Baja California").
ELSEWHERE_WHOLE = re.compile(r"\W*((por favor,? )?baja( por favor)?|(bitte )?abmelden( bitte)?|bitte abbestellen|keine werbung( bitte)?|"
                             r"band karo|(por favor,? )?no me escriba( m[aá]s)?( por favor)?|cancelar (mi )?(suscripci[oó]n|subscripci[oó]n)|"
                             r"d[eé]sinscription|d[eé]sabonnement|"
                             r"(per favore,? )?(cancellatemi|rimuovetemi|cancellami|rimuovimi)( dalla (vostra |sua |tua )?lista)?(,? (per favore|grazie))?|"
                             r"non scrivetemi pi[uù]|disiscrizione|"
                             r"((por )?favor,? )?(remover|remova|removam|retirar)( o| a)? (meu|minha)( e-?mail| endere[cç]o)?( da (sua |vossa )?lista)?(,? obrigad[oa])?|"
                             r"descadastrar|cancelar (a )?(minha )?inscri[cç][aã]o|(bitte )?austragen( bitte)?|uitschrijven|配信停止|退订|退訂|取消订阅|取消訂閱|수신거부)\W*", re.I)
# A company's own footer, not the writer speaking: "click here to unsubscribe", "unsubscribe from our newsletter", "you may
# opt out", "if you no longer wish to receive ...", an opt-out among a line of links, or the same in another language. A line
# in which someone speaks ("please", "I", a question) is theirs: see WRITER_SPEAKS.
FOOTER_LINE = re.compile(
    r"click( here)? to (unsubscribe|opt[- ]?out)|^\W*to unsubscribe,? (from|click|visit|use|follow|go to|please click|here)\b|"
    r"\bunsubscribe (here|from (our|this|these|the) (\w+ )?(newsletters?|mailings?|marketing|updates|communications?|list))|"
    r"\bmanage (your|e-?mail) (\w+ )?(preferences|subscriptions?)|\bupdate your (e-?mail )?preferences|"
    r"\byou (may|can) (opt[- ]?out|unsubscribe) (of|from|at|by|here|using|via|any)\b|"
    r"\bif you (no longer|do not|don'?t|would prefer not to|would rather not)( \w+)? ?(wish |want |like )?(to )?(receive|get)\b|"
    r"\|\s*unsubscribe|unsubscribe\s*\||\bthis (e-?mail|message) was sent (by|to)\b|"
    r"\bnewsletter abbestellen|\bwenn sie\b.{0,60}\b(erhalten|bekommen) m[oö]chten|\bklicken sie hier|"
    r"\bsi no desea recibir|\bdarse de baja|\b(para|puede) dar de baja su|\bhaga clic aqu[ií]|"
    r"\bpour ne plus recevoir|\bcliquez ici|\bvous d[eé]sabonner", re.I)
SIGN_OFF = re.compile(r"^\W*(thanks|thank you|thx|regards|kind regards|best|best regards|cheers|sincerely|gracias|saludos|atentamente|danke|gr[uü](ß|ss)e?|"
                      r"mit freundlichen gr[uü](ß|ss)en|mfg|merci|cordialement|grazie|cordiali saluti|obrigad[oa]|atenciosamente)\W*$", re.I)
WRITER_SPEAKS = re.compile(r"\?|\b(please|pls|plz|kindly|i|i'm|i've|we|me|bitte|ich|por favor|quiero|je|merci de|svp|mujhe)\b", re.I)
BARE_OPT_OUT = re.compile(r"^\W*(unsubscribe|opt[- ]?out)( here)?\W*$", re.I)
# Someone angry at US is not good news, whatever single word matched. Anger at their spreadsheets or their vendor is why they
# answered ("I'm sick of spreadsheets, tell me more"), so it counts only in a sentence aimed at us.
UPSET = re.compile(r"\b(furious|disgrac\w*|outrag\w*|ridiculous|appall\w*|unacceptable|annoy\w*|fed up|sick of|pissed|angry|"
                   r"never listen|put up with|how dare|third time|told you (to stop|already|not to))\b", re.I)
AT_US = re.compile(r"(?<!thank )\byou\b|\byour\b|\b(these|this|those|the) e-?mails?\b|\bstop\b", re.I)
# An automatic reply that matters: the mailbox is dead, the person has gone, or the mail waits for us to prove who we are.
MAILBOX_GONE = re.compile(r"\b(no longer (monitored|in use|active|checked|with|at|work\w*|employed)|(has|have) left( \w+){0,2} (company|organi[sz]ation|business|firm)|"
                          r"left the (company|organi[sz]ation|business|firm)|(mailbox|address|account) (is|has been|will be) (closed|deactivated|disabled|removed)|"
                          r"only accepts mail|verified senders|verify (that )?you are|is on hold)\b", re.I)
TICKET = re.compile(r"\[?\b(request|ticket|case) received\b|\b(request|ticket|case)\s*\(?#?\w*\)? (has been|was|is) (received|created|logged|opened)|"
                    r"\ba ticket has been created|\bticket (id|number|no)\b\W*#?\d+", re.I)
# A robot's greeting, marked as automatic: nobody asked anything, whatever its question marks say.
ROBOT_GREETING = re.compile(r"\b(how can we help|do you need (urgent )?help|need help\?|office hours|business hours|"
                            r"thanks? (you )?for (contacting|your (inquiry|enquiry))|a representative will|"
                            r"we are available|we('| wi)ll get back)", re.I)
# A robot offering something ("Interested in a demo?"), not someone interested.
ROBOT_OFFER = re.compile(r"\binterested in [^.?!]{0,60}\?", re.I)
TICKET_REASON = "an automatic confirmation from their helpdesk or office: no one has answered yet, and a person checks"
# The whole message, or a line of its own: "Just stop.", "I said stop", "STOP STOP STOP", "Stop this.", "No. Stop.", "Remove pls", or a line's last word: "What is this? STOP".
JUST_STOP = re.compile(r"(?im)^\s*((please|pls|plz|just|i said|no|yes|ok|okay|sure)[ \t,.!]+){0,2}(stop|remove|delete)([ \t,.!]+stop)*([ \t]+(it|me|us|now)|(?<=stop)[ \t]+this)?"
                       r"([ \t,.!]+(please|pls|plz|thanks|thank you))*[\s.!,]*$|(?<=[.!?])[ \t]+stop[ \t.!]*$")
# "my spam folder", "it went to spam", "anti-spam": a mention, not an accusation (taken out before reading for one).
SPAM_ASIDE = re.compile(r"\bspam[ -](folders?|filters?|box(es)?|traps?|checks?|checkers?|score|blockers?|protection|settings)\b|"
                        r"\banti-?spam\b|"
                        r"\b(in|to|into|from|under|out of|check\w*) (my |the |our |your |their )?spam\b", re.I)
_LAWYER = r"(lawyers?|attorneys?|legal (team|counsel|department))"
COMPLAINT = re.compile(r"\b((?<!can-)spam\w*|report(ed|ing)? (you|this)|legal action|lawsuit|sue you|suing|harass\w*|"
                       r"cease and desist|(i|we)('ll| will| shall) sue|(f+u+c*k+|f|piss) off(?=\W*$)|get off my back|for the (last|final) time,? (please )?stop|how many times (do|must|have|should) (i|we) (have to |need to )?(tell|ask) you\b[^.?!]{0,30}\b(stop|quit|remove)|asked you[^.?!]{0,30}\bquit|"
                       r"(further|any) (contact|e-?mails?|communications?) (will|must|should)? ?(be )?(referred|directed|go(es)?) (to|through) (our |my )?(legal )?(counsel|attorney|lawyer)|direct all further communications? to (our|my) (legal )?(counsel|attorney|lawyer)|file a complaint with the (attorney general|ftc|fcc)|"
                       rf"(contact\w*|call\w*|hear from|involv\w*|(forward|send|report|refer|hand)\w* (this|it|these|you|your \w+)( over)? to) "
                       rf"(my|our|a|an) {_LAWYER}|"
                       rf"(my|our) {_LAWYER} (will|is going to|shall) (contact|call|be in touch|hear|sue)|"
                       r"(violat\w*|breach\w*|complaint\w*|illegal|unlawful|against)\W+(\w+\W+){0,4}(gdpr|ccpa|can-?spam)|"
                       r"(gdpr|ccpa|can-?spam)\W+(\w+\W+){0,2}(violations?|breach\w*|complaints?|requests?)|"
                       r"how did you get (my|this) (email|address)|who gave you (my|this|our) (e-?mail|address|contact|details)|"
                       r"(i|we)( have|'ve)? (never|not|did ?n.?t|did not) (sign(ed)? up|subscribed?|opt(ed)? in)|"
                       r"(i|we)( have|'ve)? blocked (you|your)|(marked|reported)( \w+)? as (spam|junk)|unsolicited|get lost|"
                       # a request to remove personal data: suppressed at once, and a person handles the deletion
                       r"(remov|delet|eras|purg)(e|es|ed|ing) (all )?(my|our) (personal )?(data|details|information|info)|"
                       r"right to be forgotten|ccpa|data (removal|deletion))\b", re.I)
# The same words without an accusation: no complaint and nothing suppressed, but no automation either.
LEGAL_MENTION = re.compile(r"\b(spam|lawyers?|attorneys?|legal|illegal|unlawful|gdpr|ccpa|can-?spam|sue|harass\w*)\b", re.I)
OOO = re.compile(r"\b(out of (the )?office|on (vacation|holiday|leave)|limited access to (my )?email|"
                 r"will be back|returning on|away until|automatic reply)\b", re.I)
CALENDLY = re.compile(r"calendly\.com", re.I)
# Calendly's own subject, also forwarded, in any mail program's language ("Fwd:", "WG:", "RV:", "TR:", "SV:", "AW:"); a reply in
# English ("Re: ...") never starts like this. Whether a notice is believed is decided elsewhere, and this changes nothing of that.
CALENDLY_NOTICE = re.compile(r"\s*(?:(?:fwd?|fw|wg|rv|tr|sv|aw)\s*:\s*)*(?:new event|cancel+ed|rescheduled|updated)\s*:", re.I)
CALENDLY_SUBJECT = re.compile(r"\b(new event|canceled|cancelled|rescheduled|invitee)\b", re.I)
NO_REPLY = re.compile(r"(no-?reply|do-?not-?reply)", re.I)
# What mail programs put before a subject when answering or forwarding, in the languages our prospects write in.
REPLY_WORDS = (r"re(?:\[\d{1,4}\]|\(\d{1,4}\))?|fwd?|fw|aw|wg|sv|vs|tr|rv|antw|ref|odp|r|rif|i|ynt|res|enc|ang|"
               r"回复|回覆|答复|答覆|转发|轉寄|轉發")
# Asking for a meeting, a call or a demo in everyday wording (and without the article: "schedule meeting").
_TALK = r"(talk|chat|speak|meet|connect|discuss|catch up|hop on a call|jump on a call)"
_THING = r"(time|meeting|call|demo|chat|session|appointment|slot|conversation|walkthrough|presentation)"
_WHEN = r"(monday|tuesday|wednesday|thursday|friday|tomorrow|today|this week|next week)"
# What they ask for, alongside the category (their own words only): a price always goes to a person.
PRICING = re.compile(rf"\b(pric(e|es|ing)|{_HOW_MUCH}|cost(s)?|quote|quotation|rates|fees|budget|estimate|ballpark|"
                     r"discount(s|ed)?|cheaper|rebate|coupon|\d+\s?% off)\b", re.I)
# Having no money for it says no, and asks no price: "No budget at the moment. How much is it?" still asks.
NO_BUDGET = re.compile(_NO_BUDGET, re.I)
MORE_INFO = re.compile(r"\b(more (info|information|details)|tell me more|learn more|any (info|details)|"
                       r"send (me |us )?(some |more |a |the )?(info|information|details|deck|case stud(y|ies)|brochure|"
                       r"materials?|examples?))\b", re.I)
INTENT_LABELS = {"meeting_request": "Meeting request", "pricing_request": "Pricing request", "more_info": "More information"}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
MEETING = re.compile(
    rf"\b((can|could|shall|should|would|may) (we|you|i) (please )?({_TALK}|schedule|set ?up|arrange|book|organi[sz]e)"
    rf"|(schedule|arrange|set ?up|book|organi[sz]e|plan|fix) (a |an |the |some |our )?{_THING}"
    rf"|(let'?s|lets|let us) ({_TALK}|schedule|arrange|set ?up|book|do|have)"
    rf"|(would|i'?d|we'?d|i|we) (really )?(like|love|want|be happy|be glad|be keen) (to )?(see |get |have |book |schedule |set up )?"
    rf"(a |an )?(demo|call|meeting|chat|walkthrough|presentation|quote|proposal|trial)"
    rf"|(send|share|show) (me |us )?(a |an |the )?(demo|deck|proposal|quote|calendar invite)|(?:^|(?<=[.!?,] )|(?<=please ))call me(?! (crazy|old|if|when|never|an?\b))"
    rf"|{_TALK}\b[^.?!\n]{{0,40}}\b{_WHEN})\b", re.I)
# "not", "never", "don't", "nobody" ... up to four words before an interest phrase: "not really interested", "don't send me more info",
# "I doubt we'd be interested". "but", "so" and the like end it ("not now but we are interested"), and "no doubt" is a yes.
NEGATED = re.compile(r"(\bnot|\bnever(?! (got|received|saw)\b)|\bno longer|\bcannot|\bdont|\b\w+n['\u2019]t|\bwhy would|\bnobody|\bno one|\bnone|(?<!\bno )(?<!\ba )\bdoubt|\bhardly|"
                     r"\bunlikely|\bnothing|\bno need)\s+((?!but\b|however\b|though\b|although\b|yet\b|and\b|so\b|only\b|wait\b)[\w'\u2019]+\s+){0,4}$", re.I)
HUMAN = ("interested", "not_interested", "objection", "referral", "question", "other")
OUTREACH_STAGE = {"interested": "interested", "not_interested": "not_interested"}
QUESTION_PLACEHOLDER = "[Write your answer here]"


def _flat(text: str) -> str:
    """One line with plain apostrophes: a wrapped line or a phone's curly apostrophe must not hide an opt-out."""
    return " ".join(text.replace("\u2019", "'").split())


def classify(row: InboundEmail, own_addresses: frozenset[str] | set[str] = frozenset()) -> tuple[str, str, list[str]]:
    """`own_addresses`: our mailboxes and trusted forwarders, whose line above a forwarded Calendly notice never suppresses them."""
    category, confidence, reasons = _classify(row, own_addresses=own_addresses)
    if category in ("bounce", "calendly"):
        return category, confidence, reasons
    if category != "unsubscribe" and (chain := _opt_out_in_the_chain(row)):
        if category in ("interested", "question"):  # as below the quote: a person reads it, nothing is suppressed
            return category, "low", [*reasons, f"possible opt-out in a forwarded part of the chain: {', '.join(chain)}"]
        return "unsubscribe", "medium", [*chain, "written in a forwarded part of the chain: a person checks"]
    if not row.new_text and (row.body_text or "").strip():
        if confidence == "high":
            # Nothing was written above the quote: the words were found below or inside it, so a person checks first.
            return category, "medium", [*reasons, "reply written below the quoted email"]
        if category == "other" and (below_copy := below_header_copy(row.body_text)):
            below, _, why = _classify(row, below_copy)  # Outlook keeps the earlier email on top; an opt-out may follow it
            if below == "unsubscribe":
                return "unsubscribe", "medium", [*why, "written below a copied email header"]
        return category, confidence, reasons
    rest = rest_of_reply(row.body_text or "") if not row.auto_submitted else ""
    if rest and category != "unsubscribe":
        below, _, why = _classify(row, rest)
        if any(w.startswith(POSSIBLE_OPT_OUT) for w in why):
            return category, "low", [*reasons, *(w for w in why if w.startswith(POSSIBLE_OPT_OUT))]
        if below == "unsubscribe":
            if category in ("interested", "question"):  # they may be someone's footer: a person reads it, nothing is suppressed
                return category, "low", [*reasons, f"possible opt-out below the quote or far down: {', '.join(why)}"]
            return "unsubscribe", "medium", [*why, "written below the quoted email or far down the reply"]
        if category == "other" and below not in ("other", "out_of_office") and not unprefixed_quote(row.body_text or ""):
            return below, "medium", [*why, "reply written below the quoted email"]
    said = SPAM_ASIDE.sub(" ", _flat(f"{row.subject or ''}\n{row.new_text or own_text(row.body_text or '')}"))
    legal = LEGAL_MENTION.search(said)
    if legal and confidence == "high" and category in HUMAN:
        return category, "medium", [*reasons, f'mentions "{legal.group(0)}": a person checks']
    return category, confidence, reasons


def _opt_out_in_the_chain(row: InboundEmail) -> list[str] | None:
    """An opt-out in the middle of a forwarded chain ("Tell them to stop emailing us", from their boss), for an opt-out only. A person's
    mail only: a newsletter's or a robot's chain is nobody asking, and so is a part that holds nothing but a company's footer."""
    if row.auto_submitted or getattr(row, "mail_kind", None):
        return None
    for section in between_header_copies(row.body_text or ""):
        below, _, why = _classify(row, section)
        if below == "unsubscribe" and not any(ALL_IT_SAYS in w for w in why):
            return why
    return None


def recommended_for(row: InboundEmail) -> str | None:
    """What the lead should become, in words; a receipt is filed with the automatic replies but nobody is away."""
    if getattr(row, "mail_kind", None) == "receipt" and row.category == "out_of_office":
        return RECOMMENDED["receipt"]
    if getattr(row, "mail_kind", None) in ("delay", "sending_problem") and row.category == "bounce":  # the address is not what failed
        return RECOMMENDED[row.mail_kind]
    if getattr(row, "mail_kind", None) == "calendar" and row.category == "other":
        return RECOMMENDED["calendar"]
    if row.category == "referral":
        if passed_on(row.new_text or own_text(row.body_text or "")):
            return "Wait to hear from the person they passed it to"
        if named_addresses(row):
            return "Nothing goes to the address they gave by itself: write to them from the mailbox yourself, then log it here"
    return row.recommended_status


def intent_of(row: InboundEmail) -> str | None:
    """meeting_request | pricing_request | more_info for a person's reply (None for notices and opt-outs)."""
    if row.category not in ("interested", "question", "objection", "referral", "other"):
        return None
    text = row.new_text or own_text(row.body_text or "")
    if row.new_text and not row.auto_submitted:
        text = f"{text}\n{rest_of_reply(row.body_text or '')}"
    if PRICING.search(NO_BUDGET.sub(" ", _flat(text))):
        return "pricing_request"
    meeting = MEETING.search(text)
    if meeting and not NEGATED.search(text[:meeting.start()][-40:]):
        return "meeting_request"
    return "more_info" if MORE_INFO.search(text) else None


# They passed it on themselves, or copied someone in. Not "please forward your deck to Sam", which asks us.
PASSED_ON = re.compile(r"\b(forward(ed|ing)|pass(ed|ing)( on)?|shar(ed|ing)) (this|your|it|the)\b|\b(i'?ll|i will|we'?ll|we will) (forward|pass)\b|"
                       r"\b(looping|looped|copying|copied) in\b|\bcc'?(d|ed|ing)\b|\bcopying\b|\bon (cc|copy)\b", re.I)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A word before it, in the same sentence, that takes it back: "You needn't check back", "I won't be forwarding", "It is pointless to follow up".
_TAKEN_BACK = re.compile(r"\b(no|not|never|nor|neither|none|nobody|hardly|cannot|dont|needn'?t|\w+n't|without|pointless|useless|unnecessary|doubt|waste|avoid|instead|rather than|stop)\b", re.I)


def _before_in_sentence(said: str, at: int) -> str:
    return re.split(r"[.!?\n]", said[:at])[-1]


def passed_on(text: str) -> bool:
    """They say they passed it on or copied someone in, and do not say they did not."""
    said = _flat(text)
    return any(not _TAKEN_BACK.search(_before_in_sentence(said, m.start())) for m in PASSED_ON.finditer(said))


def named_addresses(row: InboundEmail) -> list[str]:
    """Addresses a person's reply gives, other than their own. Nothing is ever sent to them by itself: a person reads them."""
    own = {(getattr(row, field, None) or "").lower() for field in ("from_email", "to_email")}  # the mailbox it came to is ours
    found = _ADDRESS.findall(row.new_text or own_text(row.body_text or ""))
    return [a for a in dict.fromkeys(a.rstrip(".").lower() for a in found) if a not in own]


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
# Asked to come back in a month or quarter they name. Only the writer's request: "we'll get back to you in March" asks nothing of us.
CHECK_BACK = re.compile(r"\b((check|circle|come|get) back|touch base|reach (back )?out|follow[- ]?up|try (me |us )?again|(?:^|(?<=[.!?,] )|(?<=but )|(?<=please )|(?<=just ))try( me| us)?(?= (in|around|after|by) )|reconnect|revisit|"
                        r"(ping|write to|contact|email|e-mail|call) (me|us))\b( \w+){0,4}? "
                        rf"(?:(?P<prep>in|around|after|until|till|by|from)( (?P<part>early|mid|late|the start of|the end of))?[ -]"
                        rf"(?:(?P<month>{'|'.join(_MONTHS)})\b( (?P<year>20\d\d)\b)?|(?P<quarter>q[1-4])\b( (?P<qyear>20\d\d)\b)?|(?P<coming>(the )?new year|next quarter)\b)|"
                        r"(?P<bare>next quarter)\b)", re.I)
_MONTH_WORD = rf"(?:{'|'.join(_MONTHS)}|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)"
# An opt-out's words with a date after them: "don't email me until April", "stop emailing me until the new year". Not for good: the sequence
# stops, nothing is suppressed, and a person gets a reminder for the date. "until I contact you", "until further notice" name no date and stay
# opt-outs, and so does a date with "ever" or "at all" anywhere behind it ("until April. Actually never."). A date in another sentence is not
# this: it has to follow the words. "Don't email me after January" asks for silence from that date on: that is no date to write on, so it stays an
# opt-out, and so does "before Q3, none after either", and a refusal or threat behind the date ("and after that also no", "and then stop again", "or we
# will report it"). "until April, and after that feel free" is a date.
NOT_UNTIL = re.compile(
    r"\b((do not|don'?t)( ever)? (e-?mail|contact|message|write( to)?|reach out( to)?|send)( (me|us))?( any| anything| any more)?( more| further| other)?( e-?mails?| messages?| mails?| correspondence)?|"
    r"(stop|quit|cease|discontinue) (e-?maili?ng|contacti?ng|sending|messaging|mailing|writing( to)?)( (me|us))?( all| the| these| those| your| any)?( e-?mails?| messages?| mail)?|"
    r"(stop|quit|cease|discontinue)( with)?( all| the| these| those| your)? (e-?mails?|messages?)|no (more|further) (e-?mails?|messages?|contact|mails?)( please)?)"
    r"( again| any ?more)?,? (?P<prep>(until|till|til) after|until|till|til|before)( (?P<part>early|mid|late|the start of|the end of))?( the)?[ -]"
    rf"(?:(?P<month>{_MONTH_WORD})\b( (?P<day>\d{{1,2}})(st|nd|rd|th)?\b)?(,? (?P<year>20\d\d)\b)?|"
    rf"(?P<dayfirst>\d{{1,2}})(st|nd|rd|th)?( of)? (?P<month2>{_MONTH_WORD})\b|(?P<numeric>0?[1-9]|1[0-2])/(?P<numday>\d{{1,2}})(/\d{{2,4}})?\b|"
    r"(?P<quarter>q[1-4])\b( (?P<qyear>20\d\d)\b)?|(?P<coming>(the )?new year|next quarter|next year)\b)"
    r"(?![\s\S]*\b(ever|never|at all|for good|not even|on second thought|actually,? (no|not|do not|don'?t)|none after|n?or after|after either|"
    r"(no|not|don'?t) [^.!?]{0,40}\bafter (it|that)|after that\W+(\w+ ){0,2}(no|never|none|nothing|stop|remove|don'?t|do not|not either)|"
    r"stop again|then stop|or else|report (it|this|you|them)|(i|we)('ll| will| shall) (report|block|complain|escalate))\b)", re.I)
# Someone else will write, or they will: "Our CFO will get back to you in March", "Sam can reach out in March". Not "you can", not "can we".
_SOMEONE_WILL = re.compile(r"\b(?!you\b|we\b)[\w']+('ll| will| shall| would| might| may| is going to| are going to| can| could)( \w+){0,2} $|"
                           r"\b(i|we)('ll| will| shall| am going to| are going to)( \w+){0,2} $", re.I)
# What may follow the date for it to stay a date: nothing, a courtesy, a sign-off, why, or when to write again. Anything else behind it keeps the opt-out:
# "until March and after that too", "or we will go to the FTC", "and keep it that way". So does a date no reminder can mean ("until May 2099").
_AFTER_A_DATE_REFUSES = re.compile(r"\b(ever|never|forever|permanent\w*|for good|keep it that way|same|too|as well|also|beyond|still|won'?t|no|not|don'?t|do not|stop\w*|remov\w*|delet\w*|unsub\w*|"
                                   r"done|sick|tired|hate\w*|fed up|serious|annoy\w*|spammers?|pests?|idiots?|morons?|leave me|last warning|warning|i mean it|complain\w*|ftc|fcc|regulator\w*|lawyer\w*|attorney\w*|su(e|ing)|report (it|this|you|them)|block\w*|spam|or(?! (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)))\b", re.I)
_AFTER_A_DATE_FINE = re.compile(r"^\W*((please|pls|plz|thanks?( you)?|thx|cheers|regards|kind regards|best|best regards|sincerely)\b.*)?$|"
                                r"\b(then|after that|by then|at that point|afterwards?)\b.*\b(feel free|talk|chat|try|call|write|e-?mail|reach|contact|happy|open|opens|free|fine|okay|ok|good|meet|speak|connect)\b|"
                                r"\b((i'?m|i am|we'?re|we are|i'?ll be|i will be|we'?ll be|we will be)( \w+){0,2} (on leave|on vacation|on holiday|travell?ing|away|busy|swamped|buried|out|off|free|closed|moving|in the middle)|i'?d like|try (me|us) again|"
                                r"budget|on leave|travell?ing|holiday\w*|vacation|busy|or (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\b", re.I)
_NOTICE_BEGINS = re.compile(r"\b(?:confidential\w*|privileged|disclaimer|if you (?:are|have|received|got)|if received|this (?:e-?mail|message) (?:is|may|was)|sent from my)\b", re.I)


def _not_until(said: str) -> re.Match | None:
    """An opt-out's words with a date a person can write on, and nothing behind it that takes that back (see NOT_UNTIL)."""
    for found in NOT_UNTIL.finditer(said):
        behind = _NOTICE_BEGINS.split(re.sub(r"^,?\s*20\d\d\b", "", said[found.end():]), maxsplit=1)[0]  # "until 15 March 2027": the year is the date's
        if _AFTER_A_DATE_REFUSES.search(behind) or not _AFTER_A_DATE_FINE.search(behind):
            continue
        if any(int(year) > utcnow().year + 2 for year in re.findall(r"\b20\d\d\b", said[found.start():found.end() + 6])):
            continue
        return found
    return None


_PART_DAY = {"early": 1, "the start of": 1, "mid": 15, "late": 25, "the end of": 25}
_PART_WORD = {"early": "early ", "the start of": "early ", "mid": "mid-", "late": "late ", "the end of": "late "}


def check_back_when(row: InboundEmail) -> dict | None:
    """What they ask: {month 1-12, year or None, part of the month, after: past that month, words: as we write it}. None when nobody asks
    us to: a word that takes it back or someone else's promise makes no promise of ours, and no task."""
    said = re.sub(r"\btry( me| us)? (q[1-4])\b", r"try\1 in \2", _flat(row.new_text or own_text(row.body_text or "")), flags=re.I)
    for asked in CHECK_BACK.finditer(said):
        before = re.split(r"\bbut\b|\bnot (?:right )?now\b,?", _before_in_sentence(said, asked.start()), flags=re.I)[-1]  # "not now but try in Q2" asks
        if _TAKEN_BACK.search(before) or _SOMEONE_WILL.search(before):
            continue
        return _when_of(asked)
    # "don't email me until April": the words that read as a refusal above are the request here, also when written below the quote
    not_until = _not_until(said) or _not_until(_flat(rest_of_reply(row.body_text or "")))
    return _when_of(not_until) if not_until else None


def _when_of(asked: re.Match) -> dict:
    """What a match of CHECK_BACK or NOT_UNTIL names, in the form check_back_when gives it. A quarter is its first month, or its last with
    "after"; one under way is now; "next quarter" and "the new year" are the first month of it."""
    found, now = asked.groupdict(), utcnow()
    after = "after" in (found["prep"] or "").lower()
    if found["quarter"]:
        quarter, year = int(found["quarter"][1]), found["qyear"]
        first = 3 * quarter - 2
        month = first + 2 if after else now.month if not year and first <= now.month <= first + 2 else first
        return {"month": month, "year": int(year) if year else None, "part": "", "after": after,
                "words": f"{'after' if after else 'in'} Q{quarter}" + (f" {year}" if year else "")}
    coming = (found["coming"] or found.get("bare") or "").lower()
    if "quarter" in coming:
        return {"month": ((now.month - 1) // 3 + 1) * 3 % 12 + 1, "year": None, "part": "", "after": False, "words": "next quarter"}
    if "new year" in coming:
        return {"month": 1, "year": None, "part": "", "after": False, "words": "in the new year"}
    if coming:
        return {"month": 1, "year": now.year + 1, "part": "", "after": False, "words": "next year"}
    word, year, part = found["month"] or found.get("month2"), found.get("year"), (found["part"] or "").lower()
    day = int(found.get("day") or found.get("dayfirst") or found.get("numday") or 0)
    month = int(found["numeric"]) if found.get("numeric") else [m[:3] for m in _MONTHS].index(word[:3].lower()) + 1
    part = part or ("late" if day > 15 else "mid" if day > 1 else "")  # never earlier than the day they said, near enough for a reminder
    name = _MONTHS[month - 1].capitalize() + (f" {year}" if year else "")
    return {"month": month, "year": int(year) if year else None, "part": part, "after": after,
            "words": f"after {name}" if after else f"in {_PART_WORD.get(part, '')}{name}"}


def check_back_month(row: InboundEmail) -> int | None:
    """The month (1-12) they ask to hear from us again in, if they name one."""
    when = check_back_when(row)
    return when["month"] if when else None


def check_back_refused(row: InboundEmail) -> bool:
    """They name a month and say not to, or that someone else will: asking whether to check back would be asking what they just answered."""
    return bool(CHECK_BACK.search(_flat(row.new_text or own_text(row.body_text or "")))) and not check_back_when(row)


UNSUBSCRIBE_SUBJECT = re.compile(r"^\s*((re|fwd?|aw)\s*:\s*)*(unsubscribe|opt[- ]?out|remove me|stop)\s*[.!]?\s*$", re.I)

# How each kind of reply reads, and what the lead should become: shown on Replies for the person who reviews it.
SENTIMENT = {"interested": "positive", "referral": "positive", "calendly": "positive", "question": "neutral",
             "out_of_office": "neutral", "bounce": "neutral", "other": "neutral", "objection": "negative",
             "not_interested": "negative", "unsubscribe": "negative"}
RECOMMENDED = {
    "interested": "Opportunity: answer and offer a meeting",
    "referral": "Write to them from the mailbox yourself, then log it here",
    "question": "Answer the question; keep the lead active",
    "objection": "A person answers the objection",
    "not_interested": "Close as not interested; nurture later",
    "unsubscribe": "Do not contact: address suppressed",
    "bounce": "Address invalid: find another contact",
    "delay": "Still being tried: nothing to do yet",
    "sending_problem": "Our own sending was refused: check the mail server",
    "out_of_office": "Wait until they are back",
    "receipt": "Nothing to do: the email arrived",
    "calendly": "Meeting booked: prepare for it",
    "calendar": "Answer the invitation in your calendar and record the meeting: nothing is booked by itself",
    "other": "A person reviews it",
}


def sentiment_and_recommendation(category: str, intent: str | None) -> tuple[str, str]:
    if intent == "meeting_request":
        return "positive", "Opportunity: book the meeting they asked for"
    if intent == "pricing_request":
        return "positive", "A person answers the pricing question (prices always need a person)"
    return SENTIMENT.get(category, "neutral"), RECOMMENDED.get(category, RECOMMENDED["other"])


FOOTER_REASON = "possible opt-out in a footer"
OWN_BUSINESS_REASON = "possible opt-out, or their own business"
NOT_UNTIL_REASON = "possible opt-out with a date"
POSSIBLE_OPT_OUT = (FOOTER_REASON, OWN_BUSINESS_REASON, NOT_UNTIL_REASON)  # nothing is suppressed on these, wherever in the email they are read
ALL_IT_SAYS = "is all it says"
WEAK_NO = re.compile(r"\b(we'?re good|we'?re all set|no need)\b", re.I)
# A legal notice about this very email, addressed to whoever got it by mistake: "If you received this in error, delete it from your system."
# Only that phrase is blanked, only in a paragraph (hard wraps and all) that is such a notice and in which nobody speaks for themselves: "This
# is my old address. Remove it from your system." and "I am not the intended recipient, please remove me" stay what they are. A notice sits below
# what the writer says or names itself one: "If you got this address by mistake, remove it from your records" as all they say is them asking.
NOTICE_TO_YOU = re.compile(r"\b(if|should|in case) you\b.{0,200}?\b(in error|by mistake|intended|addressee)\b|\byou are not the (intended|addressee)\b|"
                           r"\bif received (in error|by mistake)\b|\bif (this|the)( e-?mail| message| communication)?( has| was| is)? (reached|sent to|not intended for) you\b|\bnot intended for you\b", re.I)
NOTICE_SPEAKER = re.compile(r"\b(i|i'm|i've|i'd|me|my|mine|we are(?! not (liable|responsible))|we were|we received|we got|we're(?! not (liable|responsible)))\b", re.I)
NOTICE_LEGAL = re.compile(r"\b(confidential\w*|privileged?|proprietary|disclaimer|recipients?|addressee)\b", re.I)
NOTICE_REMOVAL = re.compile(r"\b(remove|delete|erase)\s+(it|this|them|these|that|same|copies|e-?mails?|messages?)\s+from\s+(your|all)\s+(records?|systems?|files?)\b|\bdestroy\s+all\s+copies\b", re.I)
# Their own business in opt-out words: "we need to stop sending invoices by hand", "our unsubscribe rate", "get us off our old ERP". Taken out
# of the text before reading it again, and only here: the opt-out wording itself is not narrowed, and any other opt-out in the email still counts.
# Each one names what it is about, because anything less may be an opt-out: "we want to stop emailing with you", "no further contact with the CEO",
# "take us off old lists", "your newsletter unsubscribe link is broken" all stay what they were.
_THEIR_PAPERWORK = (r"(decks?|invoices?|bills?|files?|documents?|attachments?|pdfs?|contracts?|proposals?|slides?|reports?|quotes?|spreadsheets?|statements?|"
                    r"receipts?|faxes|purchase orders?|paperwork)")
# Only with a product named, or being built: "an opt out for our company" and "an opt-out on our account" ask for one. Nor is "don't email me the
# proposals" arranging anything until another way of getting it is named: "just bring it to the call", not "I did not ask for them".
_THEIR_PRODUCT = r"(sms|texts?|text messages?|reminders?|apps?|forms?|checkout|portal)"
_NOT_ABOUT_US = r"(?! (team|staff|people|department))(?![^.!?\n]*\b(you|your|e-?mails?|list)\b)"
_ANOTHER_WAY = (r"\b((bring|hand|print|post|upload|share|fax|send|mail|drop|give|show) (it|them|that|those|copies|a copy|me a copy)|use (the|our|my)|instead|yet|until|"
                r"(at|on|in|for) the (call|meeting|portal|drive|office)|in person)\b")
_A_LIST = r"(lists?|databases?|rosters?|director(y|ies)|registers?|records?|mail\w*|e-?mails?)"  # "take us off our vendor list" is an opt-out
_STOP_BESIDE = r"(?![\s\S]*[.!?]\s+(please |just )?stop\W*($|[.!]))(?![\s\S]*\bdone with you\b)"  # "Unsubscribe rate on this must be awful. Stop."
OWN_BUSINESS = re.compile(
    r"\b(we|i)( really| just)?( need| want| have| would like|'d like| are trying|'re trying) to (stop|quit|cease|discontinue) (e-?mailing|sending|writing) "
    rf"((out|our|the|all|all our|these|those) )*{_THEIR_PAPERWORK}\b(?![^.!?\n]*\b(to|with|from|at) (you|your)\b)|"
    rf"\b(our|their|customers'?)( newsletter)? unsubscribe (rates?|pages?|links?)\b(?![.\w-]*@)(?![^.!?\n]*\b(your|still)\b){_STOP_BESIDE}|(?<!your )(?<!your newsletter )\bunsubscribe rates?\b(?![.\w-]*@){_STOP_BESIDE}|"
    rf"\ban opt[- ]?out (on|for) (our|their)( \w+){{0,2}}? {_THEIR_PRODUCT}\b{_NOT_ABOUT_US}|"
    rf"\b(build|add|adding|offer|offering) an opt[- ]?out (on|for) (our|their)( \w+){{0,2}}? (customers?|clients?|users?|subscribers?|members?|sites?|websites?)\b{_NOT_ABOUT_US}(?![\s\S]*\bfor us\b)|"
    r"\bopt[- ]?out of (the|our)( (?!e-?mails?\b|mail\w*\b|marketing\b|contact\w*\b|list\b)\w+){0,3} (plan|contract|trial|renewal)s?\b(?![\s\S]*\b(e-?mails?|mail\w*|contact\w*|you|your)\b)|"
    rf"\b(take|get) us off ((our|old)( (?!{_A_LIST}\b)\w+){{0,2}}? (erps?|software|excel|platforms?|tools?|vendors?|providers?|process(es)?|stack|paper\w*|spreadsheets?)|our old( (?!{_A_LIST}\b)\w+)? systems?|spreadsheets?)\b(?! (mailing |e-?mail |vendor |contact |\w+ )?{_A_LIST}\b)(?![^.!?\n]*\byou\b)|"
    r"\b(if|when|once)\b(?=[^.!?\n]{0,80}?\b(trial|budget|pilot|project|contract|demo|renewal|limit)\b)((?!\b(you|your|e-?mails?|e-?mailing|mail\w*|messages?|sales|contact\w*)\b)[^.!?\n]){0,80}?\bplease stop\b|"  # "If the trial goes over budget, please stop."
    r"\bhad no further contact with (our|their|the)\b|"  # an account of the past; "no further contact with the CEO" asks for it
    rf"\b(do not|don'?t) e-?mail (me|us) (the|that|those) {_THEIR_PAPERWORK}\b(?=[^\n]{{0,60}}?{_ANOTHER_WAY})"
    r"(?![\s\S]*\b(anything|ever|never|kidding|again|any ?more|at all|stop|you keep|keep (sending|pushing)|(did not|didn'?t|never) ask|not a customer|hint)\b)", re.I)


def _is_notice(paragraph: str) -> bool:
    flat = _flat(paragraph)
    return bool(NOTICE_REMOVAL.search(paragraph) and NOTICE_TO_YOU.search(flat) and not NOTICE_SPEAKER.search(flat))


def _blank_notice_phrase(paragraph: str) -> str:
    """Only a phrase that belongs to the notice: by the end of its sentence the paragraph has told "you" what this is about."""
    def blank(found: re.Match) -> str:
        sentence_end = re.match(r"[^.!?]*", paragraph[found.end():]).end()
        return " " if NOTICE_TO_YOU.search(_flat(paragraph[:found.end() + sentence_end])) else found.group(0)
    return NOTICE_REMOVAL.sub(blank, paragraph) if _is_notice(paragraph) else paragraph


def _without_notice(text: str) -> str:
    """The text with the phrase of a legal notice blanked that asks whoever got this email by mistake to remove it (see NOTICE_TO_YOU)."""
    parts = re.split(r"(\n\s*\n)", text)
    return "".join(_blank_notice_phrase(part) if NOTICE_LEGAL.search(part) or any(p.strip() for p in parts[:i]) else part for i, part in enumerate(parts))


# Words of plain English: the rules can say that a line in English is no opt-out they know. Of a line in anything else they cannot say that.
_STRONG_ENGLISH = re.compile(r"\b(yes|okay|ok|sure|thanks|thank you|great|sounds)\b", re.I)
_ENGLISH_WORDS = re.compile(r"\b(the|this|that|is|are|was|will|would|can|could|have|has|our|your|you|we|i|i'm|i'll|to|and|for|of|on|at|with|from|it|she|he|they|my|me|us|send|please|call|again|next|week|but|not|just|try)\b", re.I)


_FOREIGN_YES = re.compile(r"^\W*(oui|sí|ja|haan|हाँ|हां)\b|(?<!no )(?<!pas )\b(me interesa|m'int[eé]resse)\b|(?<!नहीं )दिलचस्पी है", re.I)


def _cannot_be_read(text: str) -> bool:
    """A line the rules cannot place because it is not plainly English may be a refusal in words they do not know. Doubt counts as one, and a person checks.
    A signature's "Thanks" says nothing of the line above it, and nothing at all is no line to read."""
    said = _flat(" ".join(part for part in re.split(r"\n\s*\n", text) if not SIGN_OFF.match(part.partition("\n")[0])))
    if _FOREIGN_YES.search(said):
        return False
    return bool(said) and not said.isascii() or bool(said) and not (_STRONG_ENGLISH.search(said) or len(_ENGLISH_WORDS.findall(said)) >= 2)


# Words that read as no more than a mild no anywhere else, but that a person above a legal notice has always been held to: the opt-out side of a doubt.
_REFUSES_ABOVE_NOTICE = re.compile(r"\bno,? thank you\W*$|\b(i|we) (do not|don'?t) want (this|these|them|it)\b|\b(do not|don'?t) (write\b|bother\b|reply\b)|\bstop it\b", re.I)


def _their_lines(text: str) -> str:
    """Without the paragraphs that are a legal notice or start with a sign-off: an opt-out that must end its message ("Do not send anything else.") still does."""
    return "\n\n".join(part for part in re.split(r"\n\s*\n", text) if not _is_notice(part) and not SIGN_OFF.match(part.partition("\n")[0]))


def _is_signature(paragraph: str) -> bool:
    """A sign-off, or lines of nothing but a name, a title and an address: "Thanks,\\nA Person\\nVP Operations\\nwww.co.example"."""
    lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
    return len(lines) <= 8 and not any("?" in line or WRITER_SPEAKS.search(line) for line in lines) and all(
        SIGN_OFF.match(line) or all(not word[0].islower() or re.search(r"[.@\d]", word) for word in line.split()) for line in lines)


def _all_it_says_over_signature(text: str) -> str | None:
    """The first line, when all below it is a signature or a legal notice: "Abmelden", signed, over a gateway's notice, is still all they say."""
    first, _, below = text.strip().partition("\n")
    parts = [part for part in re.split(r"\n\s*\n", below) if part.strip()]
    return first.strip() if parts and all(_is_notice(part) or _is_signature(part) for part in parts) else None


def _without_footer(text: str, bare_too: bool = False) -> str:
    """The text without the lines that look like a company's footer and in which nobody speaks, and without a legal notice's removal phrase."""
    return "\n".join(line for line in _without_notice(text).split("\n") if not (
        (FOOTER_LINE.search(line) and not WRITER_SPEAKS.search(line)) or (bare_too and BARE_OPT_OUT.match(line))))


DELIVERY_WORDS = re.compile(r"\b(deliver\w*|undeliver\w*|rejected|bounced?|returned|recipient|mailbox|5\.\d\.\d)\b", re.I)


def _upset_at_us(said: str) -> re.Match | None:
    for sentence in re.split(r"[.!?]", said):
        if (upset := UPSET.search(sentence)) and AT_US.search(sentence):
            return upset
    return None


def _only_at_postmaster_a_person(row: InboundEmail, text: str) -> bool:
    """postmaster@ is often just someone at a small company: "please remove us" from there is that person, not a mail system."""
    return ((row.from_email or "").lower().startswith("postmaster@") and bool(dict(PATTERNS)["unsubscribe"].search(_flat(text)))
            and not DELIVERY_WORDS.search(text))


def calendly_sender(address: str | None) -> bool:
    """Is this address on calendly.com or a sub-domain of it? Not mycalendly.com, and not calendly.com.example."""
    address = (address or "").lower()
    return "@" in address and _within(address.rsplit("@", 1)[-1], "calendly.com")


TYPED_ABOVE_NOTICE = "their own line above the notice reads like an opt-out, and this is our own address or a trusted forwarder: a person reads it"


def _calendly_notice(row: InboundEmail, subject: str, own_addresses: frozenset[str] | set[str]) -> list[str] | None:
    """Why this is a Calendly notice, or None. From Calendly: shaped like one and in no thread of ours. Forwarded: a notice's subject and a
    calendly.com mention, unless the sender's own words above it (never the notice's) read as an opt-out, which then counts like any other.
    Our own mailbox or a trusted forwarder is relaying someone: that never suppresses them, and a person reads it."""
    if (calendly_sender(row.from_email) and not getattr(row, "in_reply_to", None)
            and (CALENDLY_NOTICE.match(subject) or CALENDLY_SUBJECT.search(subject))):
        return ["Calendly notification"]
    if not (CALENDLY.search(row.body_text or "") and CALENDLY_NOTICE.match(subject)):
        return None
    words, _, why = _classify(row, row.new_text or subject)
    if words != "unsubscribe" or any(ALL_IT_SAYS in w for w in why):
        return ["Calendly notification"]
    return ["Calendly notification", TYPED_ABOVE_NOTICE] if (row.from_email or "").lower() in own_addresses else None


def _classify(row: InboundEmail, text: str | None = None, *, own_addresses: frozenset[str] | set[str] = frozenset()) -> tuple[str, str, list[str]]:
    whole_email = text is None  # not a part of it being read again
    text = (row.new_text or own_text(row.body_text or "")) if text is None else text
    subject = row.subject or ""
    kind = getattr(row, "mail_kind", None)  # a row built by hand has none
    if kind == "sending_problem":
        return "bounce", "low", ["delivery failed because of our own sending, not because of the address"]
    if kind == "delay":
        return "bounce", "low", ["delivery delayed: the mail server is still trying"]
    if row.bounced_addresses:
        return "bounce", "high", ["delivery failure report"]
    if kind == "receipt" and not dict(PATTERNS)["unsubscribe"].search(_flat(text)):  # nobody answered: it was shown or delivered
        return "out_of_office", "high", ["read or delivery receipt"]
    if (kind == "bounce" or BOUNCE_SENDERS.match(row.from_email or "")) and not _only_at_postmaster_a_person(row, text):
        return "bounce", "low", ["delivery report: no failed address could be read from it"]
    if whole_email and (notice := _calendly_notice(row, subject, own_addresses)):
        return "calendly", "high", notice
    if not text.strip() and UNSUBSCRIBE_SUBJECT.match(subject):  # our List-Unsubscribe link: subject only, no text
        return "unsubscribe", "high", [f'subject "{subject.strip()[:60]}" with no other text']
    if not text.strip():
        return "other", "low", [CALENDAR_REASON if kind == "calendar" else "no new text of its own (only the quoted email)"]
    raw_text = text
    just_stop = JUST_STOP.search(text)
    text = _flat(text)
    said = _flat(f"{subject}\n{text}")
    complaint = COMPLAINT.search(SPAM_ASIDE.sub(" ", said))
    heard = {"referral": NOT_PASSED_ON.sub(" ", NOT_THE_RIGHT_PERSON.sub(" ", said)), "interested": OUR_AVAILABILITY.sub(" ", said)}  # never the opt-outs
    hits = {name: m.group(0) for name, rx in PATTERNS if (m := rx.search(heard.get(name, said)))}
    if just_stop:
        hits.setdefault("unsubscribe", "stop")
    opt_out_alone = _opt_out_words(_flat(f"{subject}\n{_their_lines(raw_text)}"))
    if opt_out_alone and "unsubscribe" not in hits:
        hits["unsubscribe"] = opt_out_alone.group(0)
    elsewhere = ELSEWHERE_OPT_OUT.search(said) or ELSEWHERE_WHOLE.fullmatch(raw_text.strip())
    # "Abmelden" over a gateway's legal notice is still all they say.
    beside_notice = "".join(part for part in re.split(r"(\n\s*\n)", raw_text) if not _is_notice(part)).strip()
    if not complaint and beside_notice != raw_text.strip() and ELSEWHERE_WHOLE.fullmatch(beside_notice):
        return "unsubscribe", "medium", [f'"{beside_notice}" (not in English: a person checks)']
    if not complaint and (first := _all_it_says_over_signature(raw_text)) and ELSEWHERE_WHOLE.fullmatch(first):
        return "unsubscribe", "medium", [f'"{first}" (not in English: a person checks)']
    if "unsubscribe" in hits and not (complaint or just_stop or elsewhere):
        until = _not_until(said)
        without = OWN_BUSINESS.sub(" ", f"{said[:until.start()]} {said[until.end():]}" if until else said)
        if without != said and not _opt_out_words(without):  # no other opt-out in the email: a person decides, and nothing is suppressed
            if until:
                return "objection", "low", [f'{NOT_UNTIL_REASON}: "{until.group(0)}". The sequence stops; a person reads it']
            return "other", "low", [f'{OWN_BUSINESS_REASON}: "{hits["unsubscribe"]}". The sequence stops; a person reads it']
    if ("unsubscribe" in hits or elsewhere) and not complaint:
        kept = _without_footer(raw_text, bare_too=kind == "list")
        if kept != raw_text:
            category, confidence, why = _classify(row, kept)
            if category == "unsubscribe" or any(w.startswith((OWN_BUSINESS_REASON, NOT_UNTIL_REASON)) for w in why):  # they say it themselves as well
                return category, confidence, why
            word = hits["unsubscribe"] if "unsubscribe" in hits else elsewhere.group(0)
            no_notice = _without_notice(raw_text)
            only_a_notice = no_notice != raw_text and not _opt_out_words(_flat(f"{subject}\n{no_notice}"))
            # Nothing else is said, so it is what they say; a list's newsletter says something else as well. A legal notice asks nothing of us,
            # and "We're good for now" or "We're good to go" above a company's footer does say something: it stays for a person, as before.
            theirs = _without_footer(beside_notice, bare_too=kind == "list").strip()  # what is left of it once the notice has gone too
            if (category == "other" and "unsubscribe" in hits and not only_a_notice and not WEAK_NO.search(kept)
                    and (kind != "list" or not kept.strip()) and (not theirs or _is_signature(theirs) or _cannot_be_read(theirs))):
                return "unsubscribe", "medium", [f'"{word}" {ALL_IT_SAYS}, in a line like a footer: a person checks']
            if category in ("other", "not_interested") and only_a_notice and (mild := _REFUSES_ABOVE_NOTICE.search(_flat(_their_lines(raw_text)))):
                return "unsubscribe", "medium", [f'"{mild.group(0)}" above a legal notice: a person checks']
            if only_a_notice and opt_out_alone:  # the notice was all that stood behind words that end the message
                return "unsubscribe", "high" if category == "other" else "medium", [f'"{opt_out_alone.group(0)}"']
            if category == "other" and only_a_notice and theirs and _cannot_be_read(theirs):  # it may be their no, in words the rules do not know
                return "unsubscribe", "medium", ["their own words above a legal notice cannot be read here, and they may be a refusal: a person checks"]
            return category, "low", [*why, f'{FOOTER_REASON}: "{word}". The sequence stops; a person reads it']
    negated = None
    if "interested" in hits:
        m = dict(PATTERNS)["interested"].search(heard["interested"])
        if NEGATED.search(heard["interested"][:m.start()][-40:]):
            negated = hits.pop("interested")
    meeting = MEETING.search(text)  # their own words only: our quoted email never asks them for a meeting
    if meeting and NEGATED.search(text[:meeting.start()][-40:]):  # "please don't schedule a call"
        negated = negated or meeting.group(0)
        meeting = None
    if meeting:
        hits.setdefault("interested", meeting.group(0))
    if "unsubscribe" in hits or complaint:
        reasons = [f'"{hits["unsubscribe"]}"'] if "unsubscribe" in hits else []
        if complaint:
            reasons.append(f'complaint: "{complaint.group(0)}"')
        # Honoured at once, and a person reads what they meant: "Sounds interesting" beside it, or a question above a bare
        # "Unsubscribe" line, which is often a signature's link.
        lines = [line for line in raw_text.split("\n") if line.strip()]
        also_asks = "interested" in hits or OUR_AVAILABILITY.search(said) or NAMES_WHERE_TO_GO.search(said) or TALK_INSTEAD.search(said) or (len(lines) > 1 and "?" in text and any(BARE_OPT_OUT.match(line) for line in lines))
        if also_asks:
            reasons.append("it also asks something or reads as interest: a person checks")
        return "unsubscribe", "medium" if complaint or also_asks else "high", reasons
    if elsewhere:
        return "unsubscribe", "medium", [f'"{elsewhere.group(0).strip()}" (not in English: a person checks)']
    if kind == "calendar":
        return "other", "low", [CALENDAR_REASON]
    human = [k for k in ("not_interested", "interested", "objection", "referral") if k in hits]
    if "interested" in human and (upset := _upset_at_us(said)):  # no deal is opened on it, no good news is told
        human.remove("interested")
        return (human[0] if human else "other"), "low", [f'sounds upset ("{upset.group(0)}"): a person reads it']
    if row.auto_submitted:
        return _automatic(said, text, subject, hits, human, negated, meeting)
    if TICKET.search(f"{subject}\n{text[:400]}") and not human and not negated:
        return "out_of_office", "medium", [TICKET_REASON]
    if OOO.search(f"{subject}\n{text[:400]}") and not human and not negated and "?" not in text:
        # Reads like an absence note but has no automatic-reply header: a person may have written it.
        return "out_of_office", "low", ["mentions being away, but is not marked as an automatic reply: a person checks"]
    if negated:  # "not really interested", "don't send me more info": a person reads it, nothing is automated
        reasons = [f'negated interest: "{negated}"'] + [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human]
        return (human[0] if human else "other"), "low", reasons
    if not human:
        if "?" in text:
            return "question", "medium", ["asks a question"]
        return "other", "low", [NO_CLEAR_SIGNAL]
    category = human[0]
    conflicting = len(set(human) & {"interested", "not_interested"}) == 2 or len(human) > 2
    reasons = [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human if not (k == "interested" and meeting
                                                                              and hits[k] == meeting.group(0))]
    if meeting and "interested" in human:
        reasons.append(f'asks for a meeting, call, demo or proposal: "{meeting.group(0)}"')
    if category == "not_interested" and (removal := REMOVAL_WORDS.search(said)):  # an opt-out may be meant: a person reads it, not a closed file
        return category, "low", [*reasons, f'speaks of "{removal.group(0)}": a person checks']
    return category, "low" if conflicting else ("high" if len(human) == 1 else "medium"), reasons


CALENDAR_REASON = "a calendar invitation or answer to one: a person answers it in their calendar"


def _automatic(said: str, text: str, subject: str, hits: dict, human: list, negated: str | None, meeting
               ) -> tuple[str, str, list[str]]:
    """Marked as automatic. Helpdesks and gateways put that mark on people's mail too, so only what reads as a real absence
    or says nothing to anyone stays an ordinary automatic reply; the rest goes to a person, and the sequence stops."""
    if REMOVAL_WORDS.search(said):
        return "out_of_office", "low", ["marked as an automatic reply, but it speaks of being removed: a person checks"]
    if gone := MAILBOX_GONE.search(said):
        return "out_of_office", "low", [f'automatic reply, but "{gone.group(0)}": a person checks who to write to']
    just_available = hits.get("interested", "").lower().startswith("availab") and not meeting
    interest = "interested" in human and not just_available and not ROBOT_OFFER.search(said)
    if TICKET.search(f"{subject}\n{text[:400]}") and not interest and not negated:
        return "out_of_office", "medium", [TICKET_REASON]
    if not interest and (AUTO_SUBJECT.search(subject) or OOO.search(f"{subject}\n{text[:400]}")):  # "available again on Monday"
        return "out_of_office", "high", ["automatic reply"]
    if ROBOT_GREETING.search(text) and not interest and not negated:
        return "out_of_office", "medium", [TICKET_REASON]
    words = [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human] + (["asks a question"] if "?" in text else [])
    if words or negated:
        return "other", "low", ["marked as automatic, but a person may have written it", *words]
    return "out_of_office", "high", ["automatic reply"]


AI_SYSTEM = ("You read one reply to a B2B sales email. Answer with exactly one line in the form CATEGORY|MEETING. "
             "CATEGORY is one of: interested, not_interested, question, objection, referral, unsubscribe, other. "
             "MEETING is yes if the writer asks to talk, meet, have a call or see a demo, otherwise no. "
             "Typos are common; judge the meaning. Write nothing else.")
AI_ANSWER = re.compile(r"\b(interested|not[ _]interested|question|objection|referral|unsubscribe|other)\s*\|\s*(yes|no)\b", re.I)
AI_PER_RUN = 5  # each answer takes several seconds on a local model
AI_SLOW_SECONDS = 30  # a slower (or timed-out) answer stops the second opinions for the rest of the run


def ai_second_opinion(llm, row: InboundEmail) -> tuple[str, bool] | None:
    """The local model's reading of a reply the rules could not place: (category, asks for a meeting), or None
    when no model is running or its answer is not in the expected form. Only ever a suggestion."""
    text = (row.new_text or own_text(row.body_text or "")).strip()
    if llm is None or not text:
        return None
    try:
        answer = llm.generate(f"Reply:\n<<<\n{text[:1500]}\n>>>", system=AI_SYSTEM, max_tokens=12, temperature=0.0)
    except (LLMError, OSError, ValueError):
        return None
    m = AI_ANSWER.search(answer or "")
    if not m:
        return None
    return m.group(1).lower().replace(" ", "_"), m.group(2).lower() == "yes"


def _thread_ids(row: InboundEmail) -> list[str]:
    return [*(row.in_reply_to or "").split(), *(row.references or "").split()]  # bounces: the failed email's id


def remembered_lead_id(session: Session, address: str | None, not_row: int | None = None) -> int | None:
    """The lead of someone who answered earlier in one of our threads, from any address. Being in the thread is the proof:
    a match on a subject alone, or a report's sender, is not remembered."""
    if not is_address(address):
        return None
    return session.execute(select(InboundEmail.lead_id).where(
        func.lower(InboundEmail.from_email) == address.lower(), InboundEmail.followup_id.is_not(None),
        InboundEmail.lead_id.is_not(None), InboundEmail.is_simulated.is_(False), InboundEmail.id != (not_row or 0),
        InboundEmail.category.not_in(("bounce", "calendly"))).order_by(InboundEmail.id.desc())).scalars().first()


def match(session: Session, row: InboundEmail) -> tuple[FollowUp | None, Contact | None, Lead | None]:
    ids = _thread_ids(row)
    f = None
    if ids:
        f = session.execute(select(FollowUp).where(FollowUp.message_id.in_(ids)).order_by(FollowUp.id.desc())).scalars().first()
    if f is None and row.bounced_addresses:
        f = session.execute(select(FollowUp).where(func.lower(FollowUp.to_email).in_(row.bounced_addresses),
                                                   FollowUp.status == "sent").order_by(FollowUp.id.desc())).scalars().first()
    if f is not None:
        return f, f.contact, f.lead
    contact = session.execute(select(Contact).where(func.lower(Contact.email) == (row.from_email or "").lower())
                              .order_by(Contact.id.desc())).scalars().first()
    if contact:
        lead = session.execute(select(Lead).where(Lead.contact_id == contact.id)).scalars().first() or \
            session.execute(select(Lead).where(Lead.company_id == contact.company_id)).scalars().first()
        return None, contact, lead
    earlier = remembered_lead_id(session, row.from_email, row.id)
    return None, None, session.get(Lead, earlier) if earlier else None


def company_leads(session: Session, address: str | None) -> list[Lead]:
    """The leads of the company an address belongs to, by its exact domain."""
    if not is_address(address):
        return []
    domain = (address or "").rsplit("@", 1)[-1].lower()
    return session.execute(select(Lead).join(Company, Company.id == Lead.company_id).where(
        func.lower(Company.domain) == domain, Company.merged_into_id.is_(None)).order_by(Lead.id)).scalars().all()


def subdomain_leads(session: Session, address: str | None) -> list[Lead]:
    """The leads of a company whose domain lies above this address's (us.acme.example, e.acme.example). Anyone may run a
    sub-domain, a newsletter sender most of all, so this is a reason to look, never to act."""
    if not is_address(address):
        return []
    labels = (address or "").rsplit("@", 1)[-1].lower().split(".")
    above = [".".join(labels[i:]) for i in range(1, max(len(labels) - 1, 1))]
    return session.execute(select(Lead).join(Company, Company.id == Lead.company_id).where(
        func.lower(Company.domain).in_(above), Company.merged_into_id.is_(None)).order_by(Lead.id)).scalars().all() if above else []


_SUBJECT_PREFIX = re.compile(rf"^\s*(?:(?:{REPLY_WORDS}|automatic reply|auto-?reply|out of office|undeliverable|undelivered|"
                             r"message blocked|delivery delayed|delivered|read)\s*[:：]\s*|"
                             r"(?:ticket received|return receipt \(displayed\))\s*-\s*|\[[^\]]{0,60}\]\s*)+", re.I)
_SUBJECT_TAG = re.compile(r"\s*\[[^\]]{0,60}\]\s*$")


def _bare_subject(subject: str | None) -> str:
    return " ".join(_SUBJECT_TAG.sub("", _SUBJECT_PREFIX.sub("", subject or "")).split()).casefold()


def _sent_subjects(session: Session) -> dict[str, list[tuple]]:
    index: dict[str, list[tuple]] = {}
    for lead_id, to, ours in session.execute(select(FollowUp.lead_id, FollowUp.to_email, FollowUp.subject).where(
            FollowUp.status == "sent", FollowUp.channel == "email", FollowUp.kind == "outreach", FollowUp.subject.is_not(None))).all():
        if lead_id:
            index.setdefault(_bare_subject(ours), []).append((lead_id, to))
    return index


def remember_sent_subjects(session: Session) -> None:
    """For one inbox run, which looks at up to 600 emails and sends none: the sent subjects are read once, in this session."""
    session.info["sent_subjects"] = _sent_subjects(session)


def sent_with_our_subject(session: Session, subject: str | None) -> list[tuple]:
    """(lead id, recipient) of the outreach emails whose subject this one is: "Re: X", "[Acme] RE: FW: X", "Automatic reply: X".
    A helpdesk or a personal address answers without our thread headers, so this is all that is left to go on. Only the whole
    subject counts, and only ours: a reply we sent carries the prospect's own words ("Re: Quick question")."""
    seen = _bare_subject(subject)
    if not seen:
        return []
    index = session.info["sent_subjects"] if "sent_subjects" in session.info else _sent_subjects(session)
    return list(index.get(seen, []))


def lead_from_our_subject(session: Session, row: InboundEmail) -> tuple[Lead | None, bool]:
    """(the lead this email is about, whether the sender is at that company's own domain), with no thread to go on. Someone at
    the company reaches the lead. Anyone else (a subject is easily guessed: it often names the company) or a sub-domain ties
    to it only when one lead can be meant, and that only holds its emails until a person decides."""
    sent = sent_with_our_subject(session, row.subject)
    ours = {lead_id for lead_id, _ in sent}
    for lead in company_leads(session, row.from_email):
        if lead.id in ours:
            return lead, True
    body = (row.body_text or "").lower()
    named = {lead_id for lead_id, to in sent if to and to.lower() in body}  # a quote that names who we wrote to
    candidates = named or ours or {lead.id for lead in subdomain_leads(session, row.from_email)}
    return (session.get(Lead, next(iter(candidates))) if len(candidates) == 1 else None), False


def lead_named_in_body(session: Session, row: InboundEmail) -> Lead | None:
    """The one lead whose address, an address we emailed, this mail names, with neither thread nor subject to go on: an Outlook paste under a
    new subject, from someone's personal address. Anyone can write an address, so this only ever holds that lead's emails until a person
    decides. A company's list, a robot, a no-reply sender or a sub-domain's mail holds nothing here. A quote that does not name the address
    still matches nothing, and neither does mail naming two leads."""
    if (getattr(row, "mail_kind", None) == "list" or row.auto_submitted or NO_REPLY.match(row.from_email or "")
            or sent_with_our_subject(session, row.subject) or subdomain_leads(session, row.from_email)):
        return None
    named = {a.lower() for a in _ADDRESS.findall(row.body_text or "")}  # whole addresses: one inside a longer one is another address
    if not named:
        return None
    leads = set(session.execute(select(FollowUp.lead_id).where(
        func.lower(FollowUp.to_email).in_(named), FollowUp.status == "sent", FollowUp.channel == "email", FollowUp.kind == "outreach",
        FollowUp.lead_id.is_not(None))).scalars())
    return session.get(Lead, next(iter(leads))) if len(leads) == 1 else None


def lead_on_hold(session: Session, lead_id: int | None) -> bool:
    """Someone we do not know wrote about this lead and no person has decided yet: its emails wait. Mark reviewed or reclassify
    on Replies moves the reply off review, and that releases them."""
    return bool(lead_id) and session.execute(select(InboundEmail.id).where(
        InboundEmail.lead_id == lead_id, InboundEmail.holds_sending.is_(True), InboundEmail.status == "needs_review").limit(1)).first() is not None


def unproven_in_junk(row: InboundEmail, f: FollowUp | None) -> bool:
    """Junk is where forged mail lands, and anyone can write someone's address on a From line. Only our own Message-ID in the thread
    or the sender domain's verified signature proves such an email. A delivery report has only the quoted id: its "sender" means nothing."""
    if not getattr(row, "in_junk", None):
        return False
    in_thread = f is not None and f.message_id in _thread_ids(row)
    return not in_thread and (row.category == "bounce" or not row.sender_verified)


JUNK_ALERT = "junk_unproven:"


def _hold_unproven_in_junk(session: Session, settings, row: InboundEmail, contact: Contact | None, lead: Lead | None, actor: str) -> None:
    """Nothing is suppressed, stopped, opened or drafted on it, and it counts as nobody's reply. The lead's emails wait, so a real opt-out
    is not lost, until a person decides: Mark reviewed releases them, and reclassifying it applies what they choose. Someone a person
    has already looked at does not hold a lead again by more junk mail."""
    from aime.monitoring import alert_once

    report = row.category == "bounce"
    reading = "a delivery failure" if report else (row.category or "other").replace("_", " ")
    # A company's own newsletter, saying nothing to us, is no reason to stop writing to it: in the inbox it changes nothing either.
    newsletter = getattr(row, "mail_kind", None) == "list" and reading == "other"
    tied = None if newsletter else lead or next(iter(company_leads(session, row.from_email)), None) or lead_from_our_subject(session, row)[0]
    row.followup_id, row.contact_id = None, contact.id if contact else None
    row.lead_id, row.company_id = (tied.id, tied.company_id) if tied else (None, contact.company_id if contact else None)
    row.reasons = [*(row.reasons or []), f"found in the junk folder, unsigned and outside our thread: it reads as {reading}, but it may be forged"]
    # A notice stays one, so a person can check it in Calendly and press Apply; anything else counts as nobody's answer.
    row.category = "calendly" if row.category == "calendly" else "other"
    row.confidence, row.intent, row.sentiment = "low", None, "neutral"
    row.recommended_status = "Found in junk and may be forged: a person decides"
    row.status, row.processed_at = "needs_review", utcnow()
    seen_before = session.execute(select(InboundEmail.id).where(
        InboundEmail.in_junk.is_(True), func.lower(InboundEmail.from_email) == (row.from_email or "").lower(), InboundEmail.status == "reviewed",
        InboundEmail.id != row.id).limit(1)).first()
    hold = bool(tied) and not report and not seen_before
    row.holds_sending = True if hold else None
    row.action_note = ("Found in the mailbox's junk folder, unsigned and outside our thread, so it may be forged. Nothing was changed. "
                       + (f"{tied.company.name}'s emails wait until a person decides: Mark reviewed if it is junk, or reclassify it to apply it"
                          if hold else "Mark reviewed if it is junk, or reclassify it to apply it"))
    if not newsletter and not (row.subject or "").startswith("(could not be read"):  # that one has its own alert
        where = f" about {tied.company.name}" if tied else ""
        alert_once(session, "warning", "inbox", f"An email from {row.from_email}{where} was found in the junk folder and may be forged: it reads "
                   f"as {reading}, but it is unsigned and outside our thread. Nothing was changed"
                   f"{', and that lead' + chr(39) + 's emails wait' if hold else ''}. Please decide on Replies.", settings,
                   key=f"{JUNK_ALERT}{tied.id if tied else 'none'}")
    log_activity(session, f"Email from {row.from_email} found in junk, may be forged: held for a person", actor, type_="reply",
                 company_id=row.company_id, lead_id=row.lead_id)


def _hold_lead(row: InboundEmail, lead: Lead) -> str:
    row.lead_id, row.company_id, row.holds_sending, row.status = lead.id, lead.company_id, True, "needs_review"
    # An opt-out tied to a lead counts on the company's record after Mark reviewed (outreach.company_opted_out): only reading it another way takes it back.
    how = "Reclassify it, to other if it is unrelated" if row.category == "unsubscribe" else "Mark reviewed if it is unrelated, or reclassify it"
    return f"About {lead.company.name}, from an address we do not know and with no thread: its emails wait until a person decides. {how}"


SKIPPED_MAIL_DAYS = 30


def skipped_mail(session: Session, days: int = SKIPPED_MAIL_DAYS, limit: int = 200) -> list[SkippedMail]:
    """Mail read as not part of the sales work, newest first: sender, subject and date only, so nothing vanishes without trace."""
    since = utcnow() - timedelta(days=days)
    return session.execute(select(SkippedMail).where(SkippedMail.skipped_at >= since)
                           .order_by(SkippedMail.id.desc()).limit(limit)).scalars().all()


def forget_old_skipped_mail(session: Session, days: int = SKIPPED_MAIL_DAYS) -> None:
    session.execute(delete(SkippedMail).where(SkippedMail.skipped_at < utcnow() - timedelta(days=days)))


BOOKING_NOTICE_DAYS = 3  # a booking link this old with no Calendly notice ever read means the notices go elsewhere


def booking_links_without_notice(session: Session) -> tuple[int, datetime | None]:
    """(how many booking links went out, the oldest one's date) while no Calendly notice has ever been read in the
    monitored mailbox. Calendly writes to its account owner's address, so without forwarding no booking is recorded. Only a notice
    that was applied counts: one that waits for a person, or a stranger's lookalike, recorded no booking."""
    if session.execute(select(InboundEmail.id).where(InboundEmail.category == "calendly", InboundEmail.status == "actioned",
                                                     InboundEmail.is_simulated.is_(False)).limit(1)).first():
        return 0, None
    return tuple(session.execute(select(func.count(FollowUp.id), func.min(FollowUp.sent_at)).where(
        FollowUp.purpose == "booking_link", FollowUp.status == "sent")).one())


def _signature(settings) -> str:
    return f"Best regards,\n{settings.sender_name or f'The {settings.company_name} team'}\n{settings.company_name}"


def reply_footer(settings) -> str:
    unsub = settings.unsubscribe_email or settings.smtp_from or "this address"
    return f"\n\n---\nTo stop receiving emails from us, reply with \"unsubscribe\" or write to {unsub}.\n{sender_line(settings)}"


# A shared mailbox answers under a display name such as "Sales Team": nobody there is called Sales.
MAILBOX_NAMES = re.compile(r"\b(sales|team|support|info|hello|contact|admin|office|marketing|service|services|billing|help|"
                           r"customer|no-?reply|enquir\w+|inquir\w+|desk)\b", re.I)


_NOT_A_PERSON = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|group|holdings|solutions?|technolog(y|ies)|tech|systems?|software|consulting|"
                           r"partners|labs?|studios?|agency|ventures|capital|global|international|digital|media|networks?|bank|university|institute|"
                           r"foundation|association|insurance|realty|newsletter|notifications?|updates?|recruiting|careers|accounts|finance|operations|"
                           r"hr|dept|department)\b", re.I)
_TITLE = re.compile(r"(dr|mr|mrs|ms|mx|miss|prof|professor|sir|madam|rev|hon|capt|col|gen|lt|sgt|judge)\.?", re.I)
_AFTER_A_COMMA = re.compile(r"(jr|sr|ii|iii|iv|ph\.?d|m\.?d|esq|cpa|mba|pmp|ceo|cto|cfo|coo|cmo|cio|vp|founder|owner|president|director|manager|partner)\b", re.I)
_A_NAME = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")


def _is_the_company(first: str, whole: str, lead: Lead | None) -> bool:
    """The display name is the company's: "Acme Widgets" or "Acme" from acmewidgets.com."""
    record = getattr(lead, "company", None)
    if record is None:
        return False
    first, compact = first.lower(), re.sub(r"\W", "", whole).lower()
    name = (getattr(record, "name", None) or "").lower()
    label = (getattr(record, "domain", None) or name).lower().split(".")[0]
    return (first in re.findall(r"\w+", name) or compact in re.sub(r"\W", "", name) or compact in label
            or (len(first) >= 4 and label.startswith(first)))


def first_name(display: str | None, lead: Lead | None = None) -> str | None:
    """The first name in a display name, when it plainly is one: "Lee, Pat", "Dr. Pat Lee" and "PAT LEE" are Pat. Not an initial, a
    shared mailbox, a company or an address: a wrong name in a greeting is worse than none."""
    text = re.sub(r"\(.*?\)|\[.*?\]|<.*?>|\"", " ", display or "").replace("\u2019", "'")
    text = " ".join(re.split(r"\s+(?:\||-|\u2013|\u2014|/|via)\s+", text)[0].split())
    if not text or "@" in text or MAILBOX_NAMES.search(text) or _NOT_A_PERSON.search(text):
        return None
    surname_first = False
    if "," in text:
        last, rest = (part.strip() for part in text.split(",", 1))
        surname_first = bool(rest) and " " not in last and not _AFTER_A_COMMA.match(rest)  # "Lee, Pat", but "Pat Lee, CEO"
        text = rest if surname_first else last
    words = text.split()
    titled = False
    while words and _TITLE.fullmatch(words[0]):
        words, titled = words[1:], True
    if not words or len(words) > 4 or (titled and len(words) == 1 and not surname_first):  # "Mr. Lee": that is a surname
        return None
    first = words[0]
    if not _A_NAME.fullmatch(first) or len(first) < 2 or (first.isupper() and (len(first) < 3 or len(words) == 1)):  # "P.", "TJ", "ACME"
        return None
    if _is_the_company(first, " ".join(words), lead):
        return None
    return "-".join(part.capitalize() for part in first.split("-")) if first.isupper() or first.islower() else first


# A day a person can be held to: "Friday is impossible, but Tuesday works" offers Tuesday. "Tomorrow" and "today" are not echoed: an
# approval can come days later, when they mean something else.
_CLAUSE = re.compile(r"[.,;:!?\n()]|\b(?:but|though|however|although|and)\b", re.I)
_REFUSES_A_DAY = re.compile(r"\b(not|no|never|cannot|can'?t|won'?t|don'?t|doesn'?t|isn'?t|aren'?t|wouldn'?t|impossible|unavailable|busy|booked|out|away|off|"
                            r"travell?ing|closed|bad|tough|hard|difficult|conflict|except|other than|until|till|before|after|since|last|every|by)\b", re.I)
_OFFERS_A_DAY = re.compile(r"\b(works?|good|fine|free|open|available|suits?|best|ok|okay|how about|what about|talk|chat|speak|meet|call|connect|catch up|"
                           r"let'?s|could do|can do|morning|afternoon|evening|at \d)", re.I)
_WEEKS = ("this week", "next week")


def offered_day(text: str) -> str | None:
    """The one day (or week) the text clearly offers, as it is written in a sentence, or None."""
    low = _flat(text).lower()
    for names in (WEEKDAYS, _WEEKS):
        said = [(name, clause) for clause in _CLAUSE.split(low) if clause for name in names if re.search(rf"\b{name}\b", clause)]
        refused = {name for name, clause in said if _REFUSES_A_DAY.search(clause)}
        left = {name for name, _ in said} - refused
        if len(left) > 1:
            return None
        if left:
            name = left.pop()
            offered = any(n == name and _OFFERS_A_DAY.search(clause) for n, clause in said)
            return (name.capitalize() if names is WEEKDAYS else name) if offered else None
    return None


# A plain "thanks, see you then" is the only thing a confirmation answers: anything about changing it, or any other day or time, is for a person.
_MEETING_CHANGE = re.compile(r"\b(mov(e|ed|es|ing)|re-?schedul\w*|postpon\w*|push\w*|cancel\w*|chang\w*|(can't|cannot|can not) (make|attend)|"
                             r"(won't|will not) be able|unable|conflicts?|something came up|can we do|works? better|instead|no longer works?|"
                             r"rearrang\w*|skip\w*|sick|ill|earlier|later|another|but|however|actually|unfortunately|sorry|afraid|"
                             r"not|no|never|\w+n't|may|might|please|could|would|send|need|who|what|when|where|why|how|if)\b", re.I)
# The one question a confirmation answers: talking on a day, which must then be the booked day. Any other question is for a person to answer.
_ASKS_THE_DAY = re.compile(r"\bcan we (talk|chat|speak|meet)( on)? (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*\?", re.I)
_ANOTHER_WHEN = re.compile(rf"\b(tomorrow|today|tonight|next|morning|afternoon|evening|noon|days?|weeks?|months?|quarter|year|fortnight|travell?ing|away|on leave|vacation|holiday|out of (town|office))\b|"
                           rf"\b{_MONTH_WORD}\.? \d|\b\d{{1,2}}/\d{{1,2}}\b|\b\d{{1,2}}(st|nd|rd|th)\b", re.I)
_A_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_A_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:am|pm|a\.m|p\.m)\b|\b(\d{1,2}):(\d{2})\b|\bat (\d{1,2})\b", re.I)


def _only_confirms(text: str, meeting: Meeting, zone: str) -> bool:
    """Their words say nothing that a confirmation of this meeting would ignore. Every doubt says no, and a person writes the answer."""
    said = _flat(text)
    if _MEETING_CHANGE.search(said) or _ANOTHER_WHEN.search(said) or "?" in _ASKS_THE_DAY.sub("", said):
        return False
    try:
        at = meeting.starts_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return False
    if any(day.lower() != at.strftime("%A").lower() for day in _A_DAY.findall(said)):
        return False
    for m in _A_TIME.finditer(said):
        hour, minute = m[1] or m[3] or m[5], m[2] or m[4] or "0"
        if (int(hour) % 12, int(minute)) != (at.hour % 12, at.minute):
            return False
    return True


def suggest(settings, category: str, row: InboundEmail, contact: Contact | None, lead: Lead | None = None,
            meeting: Meeting | None = None) -> tuple[str, str] | None:
    """The reply a person approves (or edits) before it goes out, in the prospect's thread. An interested reply gets
    the booking link, mentioning the day they asked for and what the call is about; one from a lead that has already
    booked (`meeting`) confirms that meeting instead of offering a second booking."""
    name = first_name(row.from_name or (contact.first_name if contact else None), lead) or "there"
    subject = row.subject or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    sign = _signature(settings)
    if category == "interested":
        from aime.meetings import booking_minutes, local_time
        minutes = booking_minutes(settings)
        day = offered_day(row.new_text or own_text(row.body_text or ""))
        record = lead.company if lead else None
        company = company_to_address(record.name, getattr(record, "website_title", None), getattr(record, "domain", None), getattr(record, "name_set_by", None)) if record else None
        about = TOPICS.get(lead.primary_service or "") if lead else None  # the words step 2 uses, not the record's
        topic = (f" about {about}{f' at {company}' if company else ''}" if about else f" with the {company} team" if company else "")
        if meeting is not None:  # in the US time zone we write in; Calendly's own emails give them theirs
            us_zone = settings.section("followup").get("send_window_timezone", "America/New_York")
            if _only_confirms(row.new_text or own_text(row.body_text or ""), meeting, us_zone):
                move_it = ("the link in your Calendly confirmation lets you move it" if meeting.source == "calendly_email"
                           else "just reply to this email")
                body = (f"Hi {name},\n\nThank you for getting back to me. Our meeting is in my calendar for "
                        f"{local_time(meeting.starts_at, us_zone)} and I look forward to it. If that time stops "
                        f"working, {move_it}.\n\n{sign}")
            else:
                body = f"Hi {name},\n\nThank you for getting back to me.\n\n{QUESTION_PLACEHOLDER}\n\n{sign}"
        elif settings.calendly_url:
            pick = (f"You mentioned {day}: you can pick any open {minutes}-minute slot then, or at another time that "
                    f"suits you, here:" if day else f"You can pick a {minutes}-minute slot that suits you here:")
            body = (f"Hi {name},\n\nThank you for getting back to me. I would be glad to talk{topic}.\n\n{pick}\n"
                    f"{settings.calendly_url}\n\nIf none of the times work, reply with a few that do and I will fit "
                    f"around you.\n\n{sign}")
        else:
            when = "" if not day else f"{day} " if day in _WEEKS else f"on {day} "
            body = (f"Hi {name},\n\nThank you for getting back to me. I would be glad to talk{topic}. Could you share "
                    f"two or three times {when}that suit you for a {minutes}-minute call?\n\n{sign}")
    elif category == "objection":
        if any(r.startswith(NOT_UNTIL_REASON) for r in row.reasons or []):  # "don't email me until April": the reminder is the answer, not an email now
            return None
        when = check_back_when(row)
        if when:  # they said when: asking again whether to write would be asking twice
            body = f"Hi {name},\n\nThank you for letting me know. I will check back {when['words']}, as you suggest.\n\n{sign}"
        elif check_back_refused(row):
            body = f"Hi {name},\n\nThank you for letting me know.\n\n{sign}"
        else:
            body = (f"Hi {name},\n\nThank you for the honest answer. Would it be useful if I checked back in a few months, "
                    f"or would you rather I did not? Either is fine.\n\n{sign}")
    elif category == "referral":
        if passed_on(row.new_text or own_text(row.body_text or "")):
            body = f"Hi {name},\n\nThank you for passing my note along. There is nothing more you need to do.\n\n{sign}"
        elif named_addresses(row):
            body = f"Hi {name},\n\nThank you for pointing me in the right direction. There is nothing more you need to do.\n\n{sign}"
        else:
            body = (f"Hi {name},\n\nThank you for pointing me in the right direction. Could you share the best way to "
                    f"reach them, or would you prefer to forward my note?\n\n{sign}")
    elif category == "question":
        body = f"Hi {name},\n\nThank you for your question.\n\n{QUESTION_PLACEHOLDER}\n\n{sign}"
    else:
        return None
    if category in ("interested", "question") and row.intent == "pricing_request":  # never an automatic price
        call = (f"If it helps, we can go through it on a short call: {settings.calendly_url}\n\n"
                if settings.calendly_url else "")
        body = f"Hi {name},\n\nThank you for asking about pricing.\n\n{QUESTION_PLACEHOLDER}\n\n{call}{sign}"
    elif category == "interested" and row.intent == "more_info":  # they asked to read something: a link to book a call is no answer
        body = f"Hi {name},\n\nThank you for getting back to me.\n\n{QUESTION_PLACEHOLDER}\n\n{sign}"
    elif (category in ("objection", "referral") and row.intent == "pricing_request"
          and "?" in (row.new_text or own_text(row.body_text or ""))):  # a plain "our budget is committed" keeps its template; a question needs an answer
        body = f"Hi {name},\n\nThank you for getting back to me.\n\n{QUESTION_PLACEHOLDER}\n\n{sign}"
    return subject[:300], body + reply_footer(settings)


BOOK_A_MEETING = "Book a meeting: approve the suggested reply with your booking link"
OPTED_OUT_WROTE_AGAIN = "They opted out earlier and wrote again: read it on Replies. No email can go unless the owner decides"
BOUNCED_WROTE_AGAIN = "Their address bounced earlier and they wrote: read it on Replies. No email can go unless the owner decides"
COMPANY_OPTED_OUT_STEP = "Someone at this company opted out: its outreach stays stopped. Decide what to do with this deal"


def _month_due(month: int, now: datetime, year: int | None = None, after: bool = False, part: str = "") -> datetime:
    """When to write, never earlier than they asked: that month (the part of it they said) in the year they said, else its next coming, or
    the first of the month after it for "after December". This month means now, unless they said a later part of it or "after"."""
    if not year or year < now.year:
        year = now.year if month >= now.month else now.year + 1
    if after:
        return datetime(year + 1, 1, 1, 14) if month == 12 else datetime(year, month + 1, 1, 14)
    return max(datetime(year, month, _PART_DAY.get(part, 1), 14), now)


def _check_back_task(session: Session, row: InboundEmail, lead: Lead, contact: Contact | None, when: dict, actor: str) -> str:
    """A task for a person, due when they said. A task only: nothing is drafted or sent when it falls due. Their words stay on the reply,
    which a request to erase them reaches: a colleague's address and words are not copied here."""
    words = when["words"]
    subject = f"{CHECK_BACK_TASK}{lead.company.name} {words}"[:300]
    if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task", FollowUp.subject == subject,
                                                 FollowUp.status == "pending").limit(1)).first():
        return f"a task to check back {words} is already open"
    session.add(FollowUp(lead_id=lead.id, contact_id=contact.id if contact else None, company_id=lead.company_id, channel="task", step=1,
                         due_at=_month_due(when["month"], utcnow(), when["year"], when["after"], when["part"]), status="pending", subject=subject,
                         body=(f"Someone at this company asked us to check back {words}: their reply is on this lead's page. First check that they have not opted "
                               "out since. Nothing goes out by itself: writing to them is a person's decision.")))
    log_activity(session, f"Task created: check back {words}", actor, type_="task", company_id=lead.company_id, lead_id=lead.id)
    return f"they asked us to check back {words}: a task is due then, and nothing goes out by itself"


def ensure_opportunity(session: Session, lead: Lead, actor: str) -> Opportunity:
    lead = lead_with_the_deals(session, lead)
    opp = session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id)  # an open deal first: a merge can put one beside a closed deal
                          .order_by(Opportunity.stage.in_(CLOSED), Opportunity.id)).scalars().first()
    if opp is None:
        from aime.knowledge.services import service_name
        opp = Opportunity(lead_id=lead.id, company_id=lead.company_id, stage="discovery", probability=20,
                          name=f"{lead.company.name} — {service_name(lead.primary_service)}", service=lead.primary_service,
                          next_step=BOOK_A_MEETING)
        session.add(opp)
        session.flush()
        opened(session, opp, actor, "interested reply")
        log_activity(session, f"Opportunity created from an interested reply: {opp.name}", actor, type_="opportunity",
                     company_id=lead.company_id, lead_id=lead.id, opportunity_id=opp.id)
    elif opp.stage == "new":
        move(session, opp, "discovery", actor, "interested reply", probability=max(opp.probability or 0, 20))
    elif opp.stage == "lost":  # they said no before and are interested now: the deal is open again
        log_activity(session, f"Opportunity stage lost → discovery (reopened; it was lost: "
                     f"{opp.lost_reason or 'no reason recorded'})", actor, type_="opportunity",
                     company_id=opp.company_id, lead_id=lead.id, opportunity_id=opp.id)
        move(session, opp, "discovery", actor, f"reopened; it was lost: {opp.lost_reason or 'no reason'}")
    return opp


# Opportunities the automation opened and no person has moved on yet. A "not interested" or opt-out reply closes
# these as lost; a deal a person has taken further (proposal, negotiation) is theirs to close.
AUTO_CLOSE_STAGES = ("new", "discovery")


def close_opportunities(session: Session, lead: Lead, reason: str, actor: str, sure: bool) -> list[str]:
    """End the lead's open deals after a clear "no": early ones are closed as lost with the reason; a later-stage
    deal, or any deal when the reading is uncertain, stays open with a next step asking a person to decide."""
    notes = []
    for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead_with_the_deals(session, lead).id,
                                                         Opportunity.stage.not_in(("won", "lost")))).scalars():
        if sure and opp.stage in AUTO_CLOSE_STAGES:
            old = opp.stage
            move(session, opp, "lost", actor, reason)
            opp.lost_reason, opp.next_step = reason[:200], None
            log_activity(session, f"Opportunity stage {old} → lost ({reason})", actor, type_="opportunity",
                         company_id=opp.company_id, lead_id=opp.lead_id, opportunity_id=opp.id)
            notes.append(f"opportunity closed as lost ({reason})")
        else:
            opp.next_step = f"They replied: {reason.lower()}. Close it as lost or keep it open"[:300]
            notes.append(f"opportunity left open in {opp.stage}: a person decides")
    return notes


def _answers_that_stay(session: Session, lead: Lead, gone: set[str]) -> tuple[int, ...]:
    """The drafts on this lead that answer an email someone really wrote from an address that did not fail. A delivery report says nothing
    about them. An approved one goes back to a person, as it would otherwise go on the next job with only its address checked."""
    from aime.automation import return_to_person

    stay = []
    for draft in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.kind == "response",
                                                        FollowUp.status.in_(("awaiting_approval", "approved")))).scalars():
        if ((draft.to_email or "").lower() in gone or is_suppressed(session, draft.to_email) or (draft.contact and draft.contact.consent_status == "opted_out")
                or not session.execute(select(InboundEmail.id).where(InboundEmail.suggested_followup_id == draft.id, InboundEmail.is_simulated.is_(False)).limit(1)).first()):
            continue
        if draft.status == "approved":
            return_to_person(draft, "a delivery failure report arrived on this lead")
        stay.append(draft.id)
    return tuple(stay)


def _act_on_bounce(session: Session, settings, row: InboundEmail, f: FollowUp | None, contact: Contact | None,
                   lead: Lead | None, person: bool, actor: str) -> list[str]:
    """Anyone can send a delivery report, so only what can be checked against what we sent counts. An address is suppressed
    only as the recipient of the email whose Message-ID the report quotes, which a stranger cannot know. If the report quotes
    our email but names someone else (an alias's target), or a mail system names an address we wrote to and quotes nothing, the
    sequence stops and a person decides. A person who reclassifies a report as a bounce confirms the failed email's address."""
    notes = []
    named = [a.lower() for a in row.bounced_addresses or []]
    kind = getattr(row, "mail_kind", None)
    if kind == "sending_problem" and not person:
        from aime.monitoring import alert_once

        reported = {str(a).lower() for found in session.execute(select(InboundEmail.bounced_addresses).where(
            InboundEmail.mail_kind == "sending_problem", InboundEmail.status.in_(("new", "needs_review")))).scalars() for a in found or []}
        alert_once(session, "error", "inbox", f"Mail to {len(reported | set(named)) or 'an'} address(es) was refused or limited because "
                   f"of our own sending (a limit exceeded, or our address or server blocked), not because of their address. Latest: "
                   f"{row.mail_note or 'no reason given'}. Nothing was suppressed. Please check the mail server.",
                   settings, key="mail:refused_for_us")
        row.status = "needs_review"
        return ["Refused because of our own sending: their address is left as it is, and the owner is alerted"]
    held_back = False
    if person:
        failed = named or ([f.to_email] if f and f.to_email else [])
        certain = True
    else:
        quoted = f is not None and f.message_id in _thread_ids(row)
        failed = [a for a in named if quoted and a == (f.to_email or "").lower()]
        certain = bool(failed)
        wrote_to = BOUNCE_SENDERS.match(row.from_email or "") and named and session.execute(select(FollowUp.id).where(
            func.lower(FollowUp.to_email).in_(named), FollowUp.status == "sent").limit(1)).first()
        if not failed and ((quoted and kind == "bounce") or wrote_to):
            held_back = True
            if lead and lead.outreach_status:
                keep = _answers_that_stay(session, lead, set(named))
                if keep:  # someone there is still answering: the stage says so
                    cancel_queued(session, lead, "A delivery failure report that a person has to confirm", keep)
                    notes.append(_ANSWER_STAYS)
                else:
                    stop_sequence(session, lead, "stopped", "A delivery failure report that a person has to confirm", actor)
            notes.append("A delivery failure report: the sequence is stopped, and nothing is suppressed until a person "
                         "confirms it (reclassify it as a bounce)")
    for addr in failed:
        for c in session.execute(select(Contact).where(func.lower(Contact.email) == addr.lower())).scalars():
            c.email_status = "bounced"
        suppress(session, addr, "Bounced (delivery failure report)", bounce=True)
        notes.append(f"{addr} bounced: marked bounced and suppressed")
    if failed and f is not None and (f.to_email or "").lower() in {a.lower() for a in failed}:
        f.delivery_status = "bounced"  # delivery tracking: this email did not arrive
    if failed and lead and lead.outreach_status:
        keep = _answers_that_stay(session, lead, {a.lower() for a in failed} | set(named))
        if keep:
            cancel_queued(session, lead, "Email bounced", keep)
            notes.append(_ANSWER_STAYS)
        else:
            stop_sequence(session, lead, "bounced", "Email bounced", actor)
    if named and not failed and not held_back:
        notes.append(f"A delivery report naming {len(named)} address(es) that it cannot tie to an email of ours: "
                     "nothing was changed; a person checks it")
    if not named and not failed and not held_back:
        notes.append("Could not read a failed address from this delivery report, so nothing was suppressed. It may be a "
                     "delay or a failure: read it, and reclassify it as a bounce if delivery failed")
    row.status = "actioned" if failed and certain and (f or contact) else "needs_review"  # unmatched: a person checks
    keeper = _open_lead_instead(session, lead) if lead else None  # a reply on a merged lead shows on its company's open lead
    wrote = session.execute(select(InboundEmail).where(
        InboundEmail.lead_id.in_([lead.id, *([keeper.id] if keeper else [])]), InboundEmail.id != row.id, InboundEmail.is_simulated.is_(False), InboundEmail.category.in_(HUMAN),
    ).order_by(InboundEmail.id.desc())).scalars().first() if lead and not person else None
    if wrote:  # the report must not bury a conversation
        row.status = "needs_review"
        notes.append(f"{wrote.from_email} replied on this lead (reply #{wrote.id}, {wrote.category.replace('_', ' ')}): a person reads them together")
    return notes


_ANSWER_STAYS = "the answer to someone else there still waits for your approval: the stage stays"


def _tie_to_lead(row: InboundEmail, lead: Lead) -> None:
    row.lead_id, row.company_id = lead.id, lead.company_id


def _open_lead_instead(session: Session, lead: Lead) -> Lead | None:
    """The company's open lead when a reply reached a merged one: the merged lead has no page in the lists, so a person sees it there."""
    if lead.status != "merged":
        return None
    keeper = primary_lead(session, lead.company_id)
    return keeper if keeper is not None and keeper.status in OPEN_LEAD_STATUSES else None


def _meeting_on(session: Session, *leads: Lead | None) -> Meeting | None:
    """The next meeting on the first of these leads that has one: a merge leaves a meeting on the merged lead."""
    from aime.meetings import next_meeting

    return next((m for m in (next_meeting(session, x.id) for x in leads if x is not None) if m), None)


def _only_bounced(session: Session, row: InboundEmail, contact: Contact | None) -> bool:
    """All that stands against this sender is a delivery failure: only such list entries, no unsubscribe mail from them, and any opt-out mark on the
    contact is the one a bounce leaves on that same address. Every doubt says they opted out."""
    address = (row.from_email or "").lower()
    standing = [r for r in session.execute(_entries_covering(address)).scalars() if r.lifted_at is None]
    if not standing or not all(r.reason.startswith(BOUNCED_REASON) for r in standing):
        return False
    if contact is not None and contact.consent_status == "opted_out" and not (contact.email_status == "bounced" and (contact.email or "").lower() == address):
        return False
    return session.execute(select(InboundEmail.id).where(func.lower(InboundEmail.from_email) == address, InboundEmail.category == "unsubscribe", InboundEmail.id != row.id).limit(1)).first() is None


def _speaks_for(lead: Lead, *addresses: str | None) -> bool:
    return bool(lead.contact and lead.contact.email) and lead.contact.email.lower() in {a.lower() for a in addresses if a}


SUGGESTED_WAITS = "suggested reply waiting for your approval"
KEPT_ANSWER = "the answer to their earlier email (#{}) still waits for your approval: read this one first"
_KEPT_ANSWER = re.compile(r"the answer to their earlier email \(#(\d+)\) still waits for your approval: read this one first")
NO_CLEAR_SIGNAL = "no clear signal"
# Stages a line that says nothing new does not lower: what they said before still stands.
STAGES_THAT_STAND = ("interested", "meeting")


def _stop_beside(session: Session, other: Lead, stage: str, reason: str, actor: str) -> bool:
    """Someone else at the company wrote, and this lead's stage should not say what they did. A live conversation keeps its stage and its
    answers, with an approved answer back before a person, and only its outreach is cancelled: True then, so a person is told. A stage that
    closes a lead stays what it is."""
    from aime.automation import return_to_person

    if other.outreach_status in STAGES_THAT_STAND and stage not in ENDS_CHECK_BACK:
        for f in session.execute(select(FollowUp).where(FollowUp.lead_id == other.id, FollowUp.channel == "email",
                                                        FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
            if f.kind != "response":
                f.status, f.blocked_reason = "cancelled", reason[:300]
            elif f.status == "approved":
                return_to_person(f, reason)
        return True
    if other.outreach_status in ENDS_CHECK_BACK:
        cancel_queued(session, other, reason)
    else:
        stop_sequence(session, other, stage, reason, actor)
    return False


def _stop_the_company(session: Session, settings, row: InboundEmail, lead: Lead | None, contact: Contact | None, merged_on: bool, actor: str) -> bool:
    """An opt-out from someone on file speaks for the company until a person decides: its other leads stop as well, and nothing more is
    suppressed. reopen_refusal then refuses to reopen them, so no note here promises that. Goes by the company of the lead or contact, as
    a personal address has no company's domain, and by the words they wrote, as the classifier read them: no quote is searched here.
    `merged_on`: the reply reached a merged lead, whose open lead is that same person when its contact is who wrote or who was written to.
    Returns whether a person has to look."""
    company_id = lead.company_id if lead else contact.company_id if contact else None
    others = session.execute(select(Lead).where(Lead.company_id == company_id, Lead.id != (lead.id if lead else 0)).order_by(Lead.id)).scalars().all() if company_id else []
    reason = f"{row.from_email} at this company asked not to be contacted"
    decide, standing = [], []
    told = lead_with_the_deals(session, lead).id if lead else None  # close_opportunities has told that deal already
    for other in others:
        same = merged_on and other.status != "merged" and _speaks_for(other, row.from_email, contact.email if contact else None)
        if _stop_beside(session, other, "unsubscribed" if same else "stopped", reason, actor):
            standing.append(other)
            continue
        close_check_back_tasks(session, other, "Someone at this company asked not to be contacted")
        if other.status != "merged" and not same:
            decide.append(other)
        for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == other.id, Opportunity.lead_id != told, Opportunity.stage.not_in(CLOSED))).scalars():
            opp.next_step = COMPANY_OPTED_OUT_STEP
    if standing:
        from aime.monitoring import alert_once

        names = ", ".join(f"#{x.id} ({x.outreach_status})" for x in standing)
        alert_once(session, "warning", "inbox", f"{row.from_email} at {standing[0].company.name} opted out while another lead there is in a live conversation: lead {names}. "
                   "Its stage is left as it is and its queued outreach is cancelled. Please read it on Replies.", settings, key=f"company_opt_out:{company_id}")
    return bool(decide or standing)


# The only replies that say nothing new: a short acknowledgement, or that a colleague comes along. Anything else, in any language, stops
# everything as every reply does: a list of what may mean no would always miss one ("Go away", "We'll pass", "Dejen de escribirme").
_ACK = r"(thanks( a lot| so much| very much)?|thank you( so much| very much)?|many thanks|ok|okay|sounds good|sounds great|great|perfect|got it|noted|received)"
_JOINING = (r"((my|a|our) colleague (will|can|may|might|would like to|is going to) (also )?(join|be joining|attend)( us| me| the call| the meeting| in| too| as well)*|"
            r"my colleague is joining( us| me)?( too| as well)?|"
            r"(i'll|i will|i'd like to|i would like to) (also )?bring (along )?(my |a )colleague( along| with me| too)*)")
_BETWEEN = r"[\s,.!;:-]*"
HARMLESS = re.compile(rf"(?:(?:hi|hello){_BETWEEN})?(?:{_ACK}{_BETWEEN})*(?:(?:(?:sorry|one more thing|also|fyi|by the way|btw|just so you know){_BETWEEN})*{_JOINING}{_BETWEEN})?"
                      rf"(?:{_ACK}{_BETWEEN})*", re.I)
_SIGN_OFF = re.compile(r"[\s,.!-]*\b(best regards|kind regards|regards|best|cheers|sincerely)\W*$", re.I)
_MOST_HARMLESS_CHARS = 160
# Below a "--" line is a signature to every other rule, and a disclaimer there says "not" and "delete". An opt-out there still counts here.
SIGNED_STOP = re.compile(r"\b(stop|take off)\b", re.I)


def says_only_something_harmless(text: str, from_name: str | None) -> bool:
    """The whole text is on the short list, with at most their sign-off and their own name after it."""
    said = _flat(text).strip()
    for word in reversed(re.findall(r"\w+", from_name or "")):
        said = re.sub(rf"[\s,.!-]*\b{re.escape(word)}\W*$", "", said, flags=re.I)
    said = _SIGN_OFF.sub("", said).strip()
    return bool(said) and len(said) <= _MOST_HARMLESS_CHARS and bool(re.search(rf"{_ACK}|{_JOINING}", said, re.I)) and bool(HARMLESS.fullmatch(said))


def _nothing_new(row: InboundEmail, f: FollowUp | None, lead: Lead, person: bool) -> bool:
    """One more line from someone in the conversation that says nothing new ("my colleague will join us"). Every doubt says no, and then
    this reply stops everything as any reply does: anything off the short list, words below the quote or under a signature that may be an
    opt-out, a reading the AI finds negative, a newsletter, mail only partly read, a sender who is not proven, an address that opted out."""
    reasons = list(row.reasons or [])
    if row.category != "other" or not reasons or reasons[0] != NO_CLEAR_SIGNAL or row.is_simulated:
        return False
    if not all(r.startswith("set to other by ") or (r.startswith("AI (") and not re.search(r"suggests: (not interested|unsubscribe|objection)", r)) for r in reasons[1:]):
        return False
    if getattr(row, "mail_kind", None) or getattr(row, "in_junk", None) or row.auto_submitted or LARGE_MARK in (getattr(row, "mail_note", None) or ""):
        return False
    if lead.outreach_status in ("unsubscribed", "bounced") or not (f is not None or row.sender_verified or person):
        return False
    body = row.body_text or ""
    if rest_of_reply(body):  # they wrote below the quote too
        return False
    for part in (own_text(re.sub(r"(?m)^\s*--\s*$", "", body)), below_header_copy(body)):
        if JUST_STOP.search(part) or any(rx.search(_flat(part)) for rx in (dict(PATTERNS)["unsubscribe"], ELSEWHERE_OPT_OUT, COMPLAINT, SIGNED_STOP)):
            return False
    return says_only_something_harmless(row.new_text or own_text(body), getattr(row, "from_name", None))


def act(session: Session, settings, row: InboundEmail, actor: str) -> None:
    """Apply the CRM consequences of a classified email and record what was done."""
    label = "SIMULATED reply" if row.is_simulated else "Reply"
    person = row.status != "new"  # already looked at: a person reclassified it on Replies
    automatic_simulation = row.is_simulated and not person
    human_reply = False
    # Held by the address it names: a person's reading settles it about that lead, as with a subject, or a confirmed opt-out would stop nothing.
    held = session.get(Lead, row.lead_id) if person and row.holds_sending and row.lead_id else None
    if person:
        row.holds_sending = None  # a person has decided
    f, contact, lead = match(session, row)
    if not person and unproven_in_junk(row, f):
        _hold_unproven_in_junk(session, settings, row, contact, lead, actor)
        return
    row.followup_id = f.id if f else None
    row.contact_id, row.lead_id = (contact.id if contact else None), (lead.id if lead else None)
    row.company_id = lead.company_id if lead else (contact.company_id if contact else None)
    notes = []
    if f is not None and row.category not in ("bounce", "calendly", "out_of_office") and f.status == "sent":
        f.delivery_status = "replied"  # delivery tracking: it arrived and the person answered
    if row.category == "bounce":
        notes += _act_on_bounce(session, settings, row, f, contact, lead, person, actor)
    elif row.category == "unsubscribe":
        # Test mode (OUTBOUND_TEST_RECIPIENTS): every lead's email went to the owner's own test address, so an opt-out
        # from it speaks for the lead in that thread. Suppressing the shared test address would cancel every later
        # test email to every other lead; only the lead's own address is suppressed.
        test_address = (row.from_email or "").lower() in getattr(settings, "test_recipients", set())
        # "From: somecompany.com" names nobody: a value with no @ on the suppression list would stop a whole domain.
        real_sender = is_address(row.from_email)
        if not test_address and real_sender:
            suppress(session, row.from_email, f"Unsubscribe request by email{' (complaint)' if row.confidence != 'high' else ''}")
        if contact and contact.consent_status != "opted_out":
            contact.consent_status, contact.consent_at = "opted_out", utcnow()
        keeper = _open_lead_instead(session, lead) if lead else None
        if lead and lead.outreach_status:
            stop_sequence(session, lead, "unsubscribed", "Asked not to be contacted", actor)
        if lead:  # also a lead with no stage yet, which the line above leaves alone
            close_check_back_tasks(session, lead, "They asked not to be contacted")
        # Anyone can write "From: david@...". In our thread, or with a verified signature, it is them; otherwise the address
        # is suppressed all the same (the safe side) but only a person closes a deal on it.
        proven = f is not None or bool(row.sender_verified) or person
        unproven_deal = False
        if lead:
            # After a merge the deal may be someone else's: it closes only for the survivor's own contact, or for a deal the merge moved from the writer's
            # lead. The sequence stops all the same and a person sees it.
            deal_lead = lead_with_the_deals(session, lead)
            their_deal = deal_lead.id == lead.id or _speaks_for(deal_lead, row.from_email) or all(came_with_the_merges(session, lead, o) for o in session.execute(
                select(Opportunity).where(Opportunity.lead_id == deal_lead.id, Opportunity.stage.not_in(("won", "lost")))).scalars().all())
            closed = close_opportunities(session, lead, "Unsubscribed", actor, sure=row.confidence == "high" and proven and their_deal)
            notes += closed
            unproven_deal = bool(closed) and not proven
        if contact and contact.email and contact.email.lower() != (row.from_email or "").lower():
            # an opt-out in our thread from another address (a colleague, a personal address, test mode): the
            # address we wrote to is suppressed as well
            suppress(session, contact.email, "Unsubscribe request in the email thread")
        # "Please remove us", from someone else at a lead's company, outside our thread: it may speak for the company.
        # The sequence stops and a person decides; only the address that wrote is suppressed.
        unmatched = lead is None and contact is None and not test_address
        colleagues = company_leads(session, row.from_email) if unmatched else []
        for other in colleagues:
            stop_sequence(session, other, "stopped", f"{row.from_email} at this company asked not to be contacted", actor)
            close_check_back_tasks(session, other, "Someone at this company asked not to be contacted")
        if colleagues:
            _tie_to_lead(row, colleagues[0])
            notes.append(f"{colleagues[0].company.name}: sequence stopped; a person decides whether it speaks for the company")
        elif unmatched and (tied := lead_from_our_subject(session, row)[0] or held or (None if person else lead_named_in_body(session, row))):
            if person:  # a person says it is an opt-out about this lead
                stop_sequence(session, tied, "unsubscribed", "A person confirmed an opt-out from another address", actor)
                if tied.contact and tied.contact.email:
                    suppress(session, tied.contact.email, "Unsubscribe request confirmed by a person")
                _tie_to_lead(row, tied)
            else:  # only their own address is suppressed for now: anyone can write under a subject
                notes.append(_hold_lead(row, tied))
        company_decides = _stop_the_company(session, settings, row, lead, contact, keeper is not None, actor) if lead or contact else False
        if company_decides:
            notes.append("Everything queued at this company's other leads is stopped too, and nothing more is suppressed: someone there opted out, and a person reads it")
        if test_address:
            notes.append(f"{contact.email} suppressed (test mode: the opt-out came from the test address "
                         f"{row.from_email}, which stays deliverable)" if contact and contact.email else
                         f"test mode: the opt-out came from the test address {row.from_email} and matches no lead; "
                         "nothing suppressed, check which lead it was for")
        elif real_sender:
            notes.append(f"{row.from_email} suppressed; no further email")
        else:
            notes.append("The sender gave no usable address, so none was suppressed for it: a person checks it")
        row.status = ("actioned" if row.confidence == "high" and (contact or lead) and not colleagues and not company_decides
                      and real_sender and not unproven_deal else "needs_review")  # an address we cannot place: a person looks
        if lead:
            if keeper is not None:
                _tie_to_lead(row, keeper)
            elif lead.status == "merged":
                row.status = "needs_review"
                notes.append("this lead was merged and its company has no open lead: a person reads it")
            _meeting_is_booked(session, settings, row, keeper or lead, notes, person, _meeting_on(session, keeper, lead))
    elif row.category == "out_of_office" and row.confidence == "medium" and TICKET_REASON in (row.reasons or []):
        if lead is None and (tied := lead_from_our_subject(session, row)[0]):
            _tie_to_lead(row, tied)
        notes.append("Automatic confirmation from their helpdesk or office: the sequence continues; a person checks it")
        row.status = "needs_review"
    elif row.category == "out_of_office" and row.confidence == "high":  # marked as an automatic reply, or by a person
        receipt = getattr(row, "mail_kind", None) == "receipt"
        notes.append("Read or delivery receipt: the sequence continues" if receipt else "Automatic reply: the sequence continues")
        row.status = "actioned"
    elif row.category == "calendly":
        from aime.meetings import handle_calendly_email
        notes.append(handle_calendly_email(session, settings, row, actor))
    else:
        human_reply, by_subject = True, False
        if lead is None:  # no thread headers and an address we do not know, but our subject
            tied, colleague = lead_from_our_subject(session, row)
            tied = tied or held
            if tied and (colleague or (person and row.category != "other")):  # a person's reading settles who it is
                lead, by_subject = tied, not person
                _tie_to_lead(row, lead)
            elif tied and person:
                _tie_to_lead(row, tied)
            elif tied:
                notes.append(_hold_lead(row, tied))
            elif not person and (tied := lead_named_in_body(session, row)):
                notes.append(_hold_lead(row, tied))
            else:
                notes.append("Could not match this email to a lead")
            row.status = "needs_review"
    if human_reply and lead is not None:
        # Any human reply stops the automated sequence; the stage records what they said. A simulated one only stops it.
        from aime.meetings import _when_label, held_by_opt_out

        replied_on = lead  # its deals are the ones this reply acts on (lead_with_the_deals)
        keeper = _open_lead_instead(session, lead)
        opted_out = lead.outreach_status == "unsubscribed" or bool(keeper and keeper.outreach_status == "unsubscribed")
        # The approval gate's own test: a draft to this address could never be approved. Someone else at a company where the lead opted out is not this.
        no_writing = is_suppressed(session, row.from_email) or bool(contact and contact.consent_status == "opted_out")
        stage = "replied" if automatic_simulation or no_writing and row.category == "interested" else OUTREACH_STAGE.get(row.category, "replied")
        standing_answers = ()
        closed_beside = False  # someone else wrote about a lead that is closed: its stage and its reminders are not theirs to change
        booked = _meeting_on(session, keeper, lead)
        if keeper is not None:  # nothing queued on the merged lead survives, and the reply shows on the lead a person can see
            stays = "meeting" if lead.outreach_status == "meeting" and _meeting_on(session, lead) and row.category != "not_interested" else stage
            stop_sequence(session, lead, stays, f"{label} received on a merged lead", actor)
            for other in session.execute(select(Lead).where(Lead.company_id == lead.company_id, Lead.status.in_(OPEN_LEAD_STATUSES),
                                                            Lead.id != keeper.id).order_by(Lead.id)).scalars():
                _stop_beside(session, other, stage if _speaks_for(other, row.from_email) else "stopped", f"{label} received from someone at this company", actor)
            lead = keeper
            _tie_to_lead(row, lead)
            if not _speaks_for(lead, row.from_email):
                # Someone at the company is interested: that stage is what the Interested card and list show, unless the company has said no.
                stage = "interested" if stage == "interested" and not company_opted_out(session, lead.company) else "stopped"
                if lead.outreach_status in STAGES_THAT_STAND:  # someone else wrote: this lead's own conversation goes on
                    stage = lead.outreach_status
                    standing_answers = tuple(session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.kind == "response")).scalars())
                elif lead.outreach_status in ENDS_CHECK_BACK:
                    stage, closed_beside = lead.outreach_status, True
        if booked and row.category != "not_interested" and lead.outreach_status == "meeting":  # only a person changes it; the meeting still stands
            stage = "meeting"
        answers = []
        if _nothing_new(row, f, lead, person):  # the answer to what they asked still stands; the follow-ups stop, as for any reply
            drafts = session.execute(select(FollowUp).where(
                FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.kind == "response", FollowUp.status == "awaiting_approval",
                func.lower(FollowUp.to_email) == (row.from_email or "").lower())).scalars().all()
            # A subject of their own may say it all ("STOP", with "Thanks" below): only the conversation's own subject says nothing.
            subject = _bare_subject(row.subject)
            answers = [d.id for d in drafts if _bare_subject(d.subject) == subject]
            same_subject = bool(answers) or (f is not None and _bare_subject(f.subject) == subject)
            if same_subject and lead.outreach_status in STAGES_THAT_STAND:
                stage = lead.outreach_status
        why = f"{label} received ({row.category.replace('_', ' ')})"
        cancelled = cancel_queued(session, lead, why) if closed_beside else stop_sequence(session, lead, stage, why, actor, keep=(*answers, *standing_answers))
        if cancelled:
            notes.append(f"{cancelled} queued follow-up(s) cancelled")
        if answers:
            for draft in drafts:
                if draft.id in answers:
                    draft.blocked_reason = WROTE_AGAIN
            notes.append(KEPT_ANSWER.format(answers[-1]))
        lead.last_activity_at = utcnow()
        if row.category == "interested" and automatic_simulation:
            msg = f"SIMULATED: Interested reply from {row.from_email} ({lead.company.name})"
            session.add(Alert(level="info", source="inbox", message=msg[:500]))
            notes.append("SIMULATED: no deal opened and no draft written")
        elif row.category == "interested" and not no_writing:  # otherwise no deal opens and a lost one stays lost: they cannot be written to
            opp = ensure_opportunity(session, replied_on, actor)
            if opp.next_step in (BOOK_A_MEETING, BOOK_ASKING_TIMES):
                if booked:
                    opp.next_step = f"Meeting {_when_label(booked)}: prepare, then record the outcome"
                elif row.intent in ("pricing_request", "more_info"):
                    opp.next_step = WRITE_THE_ANSWER
                elif not settings.calendly_url:
                    opp.next_step = BOOK_ASKING_TIMES
            notes.append(f"opportunity {opp.stage}")
            if (lead.status == "nurture" and row.confidence == "high" and not by_subject and not opted_out and not closed_beside and not row.is_simulated
                    and not lead_on_hold(session, lead.id) and not held_by_opt_out(session, lead, contact, row.from_email)):
                lead.status = "qualified"
                log_activity(session, "Lead nurture → qualified (a clear interested reply)", actor, type_="status_change", company_id=lead.company_id, lead_id=lead.id)
                notes.append("clear interest: lead back from nurture to qualified")
            if not row.is_simulated:
                from aime.handover import handover
                handover(session, settings, opp, f"interested reply from {row.from_email}"
                         + (" asking for a meeting" if row.intent == "meeting_request" else ""), actor)
            if not booked:  # with a meeting booked it is no news: one alert below says a person reads it
                msg = f"{'SIMULATED: ' if row.is_simulated else ''}Interested reply from {row.from_email} ({lead.company.name})"
                session.add(Alert(level="info", source="inbox", message=msg[:500]))
                notify_event(settings, msg)
        elif row.category == "not_interested" and not automatic_simulation:
            if keeper is None or _speaks_for(lead, row.from_email):  # someone else's no does not park the open lead: they did not speak for it
                if lead.status == "qualified":
                    lead.status = "nurture"
                notes.append("lead moved to nurture")
            notes += close_opportunities(session, replied_on, "Not interested", actor, sure=row.confidence == "high")
        if no_writing and not automatic_simulation and row.category != "not_interested":
            for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead_with_the_deals(session, replied_on).id,
                                                                 Opportunity.stage.not_in(CLOSED))).scalars():
                opp.next_step = BOUNCED_WROTE_AGAIN if _only_bounced(session, row, contact) else OPTED_OUT_WROTE_AGAIN
        if not automatic_simulation and not opted_out and not no_writing and (when := check_back_when(row)):
            notes.append(_check_back_task(session, row, lead, contact, when, actor))
        proposal = suggest(settings, row.category, row, contact, lead, booked)
        written = None
        if proposal and not automatic_simulation and is_address(row.from_email) and not no_writing:
            subject, body = proposal
            draft = FollowUp(lead_id=lead.id, contact_id=contact.id if contact else None, company_id=lead.company_id,
                             channel="email", kind="response", step=1, due_at=utcnow(), status="awaiting_approval",
                             subject=subject, body=body, to_email=row.from_email, in_reply_to=row.message_id,
                             blocked_reason="Write your answer before approving" if QUESTION_PLACEHOLDER in body else None)
            session.add(draft)
            session.flush()
            written = body
            row.suggested_followup_id = draft.id
            from aime.meetings import has_booking_link

            if has_booking_link(settings, body):
                draft.purpose = "booking_link"
            elif row.category == "interested" and booked is None and not settings.calendly_url and row.intent not in ("pricing_request", "more_info"):
                draft.purpose = "ask_times"  # the same request for times in other words: a booking ends it (meetings.close_open_emails)
            notes.append(SUGGESTED_WAITS)
            if row.category == "referral" and (named := named_addresses(row)):
                notes.append(f"they gave {', '.join(named[:3])}. Nothing goes there by itself: write to them from the mailbox yourself, then log it here")
            if row.reply_to and NO_REPLY.match(row.from_email or ""):
                notes.append(f"this draft goes to a no-reply address, where nobody may read it. They ask for answers to go to "
                             f"{row.reply_to}. Nothing goes there by itself: a person decides")
        row.status = "actioned" if row.confidence == "high" and row.category in ("interested", "not_interested") else "needs_review"
        if row.intent == "pricing_request":
            row.status = "needs_review"
            notes.append("asks about pricing: a person writes the answer" if written and QUESTION_PLACEHOLDER in written
                         else "mentions pricing: a person checks the draft" if written else "mentions pricing: a person reads it")
        if row.intent == "more_info" and row.category == "interested":
            row.status = "needs_review"
            notes.append("asks for details: a person writes the answer")
        if by_subject:  # no thread headers: the company's domain and our subject are what it goes on
            row.status = "needs_review"
            notes.append("matched by the company's domain and our subject, not by our thread: a person checks it")
        if no_writing:  # still suppressed: only the owner can take that back
            row.status = "needs_review"
            only_bounced = _only_bounced(session, row, contact)
            row.recommended_status = ("Their address bounced earlier: a person reads it, and no email can go unless the owner decides" if only_bounced
                                      else "They opted out earlier: a person reads it, and no email can go unless the owner decides")
            notes.append("this address bounced earlier and stays on the do-not-contact list: a person decides" if only_bounced
                         else "they opted out earlier and stay suppressed: a person decides")
            if not person and not row.is_simulated:
                from aime.monitoring import alert_once

                alert_once(session, "warning", "inbox", f"{row.from_email} ({lead.company.name}) {'bounced earlier and now wrote' if only_bounced else 'opted out earlier and wrote again'}: read it on Replies; "
                           "no email can go unless the owner decides", settings, key=f"wrote_after_opt_out:{lead.id}")
        elif opted_out:  # someone else at the company: their own address is free, the lead's stays closed
            row.status = "needs_review"
            notes.append("this lead opted out earlier and its address stays suppressed; this sender is not on the list: a person decides")
        if closed_beside:
            row.status = "needs_review"
            notes.append(f"someone else at the company wrote, and this lead's stage stays {lead.outreach_status.replace('_', ' ')}: a person decides")
        if replied_on.status == "merged" and keeper is None:
            row.status = "needs_review"
            notes.append("this lead was merged and its company has no open lead: a person reads it")
        if not no_writing:
            _meeting_is_booked(session, settings, row, lead, notes, person, booked)
        elif booked:
            notes.append(f"a meeting is booked for {_when_label(booked)}: a person reads this")
    if getattr(row, "mail_kind", None) == "calendar" and row.category == "other" and not row.is_simulated and not person:
        who = f"{row.from_email} ({row.lead.company.name})" if row.lead_id and row.lead else row.from_email
        session.add(Alert(level="info", source="inbox", message=f"A calendar invitation or answer from {who}: please answer it in your "
                          "calendar and record the meeting. Nothing is booked by itself. It is on Replies."[:500]))
        notes.append("calendar invitation: a person answers it in their calendar and records the meeting; nothing is booked by itself")
    if getattr(row, "in_junk", None):
        notes.append("found in the mailbox's junk folder")
    if LARGE_MARK in (getattr(row, "mail_note", None) or "") and row.status == "actioned":
        row.status = "needs_review"  # what was done went by the part that was read
    if getattr(row, "mail_note", None) and row.mail_kind != "sending_problem":
        notes.append(row.mail_note)
    row.action_note = "; ".join(notes) or None
    row.processed_at = utcnow()
    log_activity(session, f"{label} from {row.from_email}: {row.category.replace('_', ' ')} ({row.confidence})",
                 actor, type_="reply", body=(row.new_text or "")[:2000], company_id=row.company_id, lead_id=row.lead_id)


def _meeting_is_booked(session: Session, settings, row: InboundEmail, lead: Lead, notes: list[str], person: bool, booked: Meeting | None = None) -> None:
    """Whatever they wrote, with a meeting booked a person reads it: they may be moving it. Nothing here touches the meeting."""
    from aime.meetings import _when_label, next_meeting
    from aime.monitoring import alert_once

    booked = booked or next_meeting(session, lead.id)
    if booked is None:
        return
    row.status = "needs_review"
    notes.append(f"a meeting is booked for {_when_label(booked)}: a person reads this")
    if not person and not row.is_simulated:
        alert_once(session, "warning", "inbox", f"{row.from_email} ({lead.company.name}) wrote while a meeting is booked for {_when_label(booked)}: "
                   "please read it on Replies. The meeting stays as it is until a person changes it.", settings, key=f"reply_with_meeting:{lead.id}")


def kept_answer_id(note: str | None) -> int | None:
    found = _KEPT_ANSWER.search(note or "")
    return int(found.group(1)) if found else None


def note_now(note: str | None, draft_status: str | None, kept_status: str | None = None) -> str | None:
    """A reply's note as it reads today: the note was written once, and its draft may have moved on since."""
    if not note:
        return note
    if SUGGESTED_WAITS in note and draft_status not in (None, "awaiting_approval"):
        note = note.replace(SUGGESTED_WAITS, f"the suggested reply no longer waits: it is {draft_status.replace('_', ' ')}")
    if _KEPT_ANSWER.search(note) and kept_status not in (None, "awaiting_approval"):
        note = _KEPT_ANSWER.sub(f"the answer to their earlier email no longer waits: it is {kept_status.replace('_', ' ')}", note)
    return note


def release_answers(session: Session, row: InboundEmail) -> None:
    """A person has read this reply: an answer that waited only for that can be approved again."""
    if not row.lead_id:
        return
    for draft in session.execute(select(FollowUp).where(FollowUp.lead_id == row.lead_id, FollowUp.kind == "response", FollowUp.status == "awaiting_approval",
                                                        FollowUp.blocked_reason == WROTE_AGAIN)).scalars():
        if not later_reply_waiting(session, draft):
            draft.blocked_reason = "Write your answer before approving" if QUESTION_PLACEHOLDER in (draft.body or "") else None


def _hold_for_a_person(session: Session, row: InboundEmail, read: tuple | None, exc: Exception, actor: str) -> None:
    """Handling this email failed. It must not stop the others and must not be lost: it goes to review with an alert,
    and what protects the person is still done. An opt-out is suppressed; any human reply stops the sequence."""
    session.rollback()
    category, reasons = (read[0], list(read[2])) if read else ("other", [])
    log.error("Reply %s could not be handled", row.id, exc_info=exc)
    try:
        found, _, lead = match(session, row)
        unproven = unproven_in_junk(row, found)  # nothing is done on the word of an email that may be forged
        if category == "unsubscribe" and is_address(row.from_email) and not unproven:
            suppress(session, row.from_email, "Unsubscribe request by email (a person checks it)")
        if category not in ("bounce", "calendly", "out_of_office") and not unproven:
            if lead:
                stop_sequence(session, lead, "unsubscribed" if category == "unsubscribe" else "replied",
                              "Reply could not be handled automatically", actor)
        session.commit()
    except Exception:  # noqa: BLE001 -- the review below still has to happen
        session.rollback()
        log.exception("Reply %s: could not stop the sequence", row.id)
    row.category, row.confidence, row.status = category, "low", "needs_review"
    row.reasons = [*reasons, f"could not be handled automatically ({type(exc).__name__})"]
    row.action_note, row.processed_at = "Could not be handled automatically: a person reads it", utcnow()
    session.add(Alert(level="error", source="inbox", message=f"A reply from {row.from_email} could not be handled "
                      f"automatically ({type(exc).__name__}): please read it on Replies."[:500]))
    session.commit()


def _conversation_turn(session: Session, settings, row: InboundEmail, llm, run: dict) -> None:
    """The hand-off to the AI conversation engine (aime.ai.conversation), after the rules have acted on this email and
    their outcome is committed. It only reads what they decided: what it records (the conversation, its facts, the
    next step, a draft for a person to approve) can never undo an opt-out, a suppression or a stop. Off unless the
    "conversation" settings section says shadow or drafts; a failure there is logged and rolled back, and the rules'
    outcome stands."""
    try:
        from aime.ai import conversation

        if conversation.mode(settings) == "off":
            return
        conversation.on_reply(session, settings, row, llm=llm, run=run)
        _turn_taken(session, row.id)  # in the engine's own commit: a turn is never taken twice
        session.commit()
    except Exception:  # noqa: BLE001 -- the engine is an addition: the inbox carries on without it
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.exception("Reply %s: the conversation engine failed; the rules' outcome stands", getattr(row, "id", None))
        try:  # a failure is final (the rules' outcome stands); only a process killed in between leaves the mark
            _turn_taken(session, row.id)
            session.commit()
        except Exception:  # noqa: BLE001 -- the mark stays and the turn is tried at the next run
            session.rollback()


# The emails whose rules' outcome is committed and whose conversation turn has not been taken yet. Written in the rules'
# own commit and cleared in the engine's, so a process killed between the two takes the turn at its next run, once.
PENDING_TURNS = "conversation.pending_turns"


def _turn_pending(session: Session, settings, row_id: int) -> None:
    from aime.ai import conversation

    if conversation.mode(settings) == "off":
        return
    waiting = list(kv_get(session, PENDING_TURNS) or [])
    if row_id not in waiting:
        kv_set(session, PENDING_TURNS, [*waiting, row_id])


def _turn_taken(session: Session, row_id: int) -> None:
    waiting = list(kv_get(session, PENDING_TURNS) or [])
    if row_id in waiting:
        kv_set(session, PENDING_TURNS, [i for i in waiting if i != row_id])


def _take_pending_turns(session: Session, settings, llm, run: dict) -> None:
    from aime.ai import conversation

    if conversation.mode(settings) == "off":  # switched off since: nothing is taken (or read) while it is off
        return
    for row_id in list(kv_get(session, PENDING_TURNS) or []):
        row = session.get(InboundEmail, row_id)
        if row is None or row.status == "new":  # gone, or not handled yet: its turn comes after the rules, as usual
            if row is None:
                _turn_taken(session, row_id)
                session.commit()
            continue
        _conversation_turn(session, settings, row, llm, run)


def process_pending(session: Session, settings, actor: str, llm=None, conversation_llm=None) -> dict:
    stats: dict[str, int] = {}
    ai_budget = AI_PER_RUN
    conversation_run: dict = {}  # the conversation engine's model budget for this run
    _take_pending_turns(session, settings, conversation_llm, conversation_run)
    for row in session.execute(select(InboundEmail).where(InboundEmail.status == "new").order_by(InboundEmail.id)).scalars().all():
        read = None
        try:
            read = classify(row, own_addresses=trusted_forwarders(session, settings) if CALENDLY_NOTICE.match(row.subject or "") else frozenset())
            row.category, row.confidence, row.reasons = read
            row.intent = intent_of(row)
            row.sentiment, row.recommended_status = sentiment_and_recommendation(row.category, row.intent)
            row.recommended_status = recommended_for(row)
            opinion = None
            if llm is not None and ai_budget > 0 and row.category in ("question", "other") and row.confidence != "high":
                ai_budget -= 1
                started = time.perf_counter()
                opinion = ai_second_opinion(llm, row)
                if time.perf_counter() - started > AI_SLOW_SECONDS:  # a stuck model must not hold up the inbox job
                    ai_budget = 0
                if opinion:
                    ai_cat, ai_meeting = opinion
                    row.reasons = list(row.reasons or []) + [
                        f"AI ({getattr(llm, 'label', 'local model')}) suggests: {ai_cat.replace('_', ' ')}"
                        + (", asks for a meeting" if ai_meeting else "") + " (please check)"]
            act(session, settings, row, actor)
            if TYPED_ABOVE_NOTICE in (row.reasons or []):
                row.status = "needs_review"
            if opinion and (opinion[0] == "interested" or opinion[1]) and row.status == "needs_review":
                where = f" ({row.lead.company.name})" if row.lead else ""
                msg = (f"{'SIMULATED: ' if row.is_simulated else ''}Possible interest{', a meeting request' if opinion[1] else ''} "
                       f"from {row.from_email}{where}: the AI reads it so, the rules were unsure. Please review it on Replies.")
                session.add(Alert(level="info", source="inbox", message=msg[:500]))
                notify_event(settings, msg)
            _turn_pending(session, settings, row.id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- one email must never stop the ones behind it, opt-outs included
            _hold_for_a_person(session, row, read, exc, actor)
        else:
            _conversation_turn(session, settings, row, conversation_llm, conversation_run)
        stats[row.category] = stats.get(row.category, 0) + 1
    return stats
