"""AIME verifies DKIM signatures itself (our mail server stamps no Authentication-Results): a Calendly notice changes
the CRM only when calendly.com's signature, or a trusted forwarder's, holds. A signature that does not verify, a
changed body or header, a revoked key, or an Authentication-Results line the sender wrote never counts. The verifier
was also checked read-only on the live mailbox (29 Oct 2026): 9 real signatures from 4 domains verified, and the same
9 messages with one body letter changed failed."""

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from sqlalchemy import select

import aime.dkim_verify as dv
from aime.models import InboundEmail, Meeting
from tests.test_inbox import FakeIMAP, contacted, run_inbox
from tests.test_meetings_pipeline import BOOKED_BODY, BOOKED_SUBJECT

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ED_KEY = ed25519.Ed25519PrivateKey.generate()


def dns_record(key) -> bytes:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return b"v=DKIM1; k=ed25519; p=" + base64.b64encode(raw)
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return b"v=DKIM1; k=rsa; p=" + base64.b64encode(der)


def sign(message: bytes, domain: str, key=RSA_KEY, selector: str = "s1", canon: str = "relaxed/relaxed",
         signed_headers=("from", "to", "subject", "date", "message-id")) -> bytes:
    """A DKIM signer for the tests (the canonical forms come from the module; the crypto does not)."""
    head_mode, body_mode = canon.split("/")
    headers, body = dv._split(message)
    bh = base64.b64encode(hashlib.sha256(dv._canon_body(body, body_mode)).digest()).decode()
    algo = "ed25519-sha256" if isinstance(key, ed25519.Ed25519PrivateKey) else "rsa-sha256"
    value = (f" v=1; a={algo}; c={canon}; d={domain}; s={selector}; h={':'.join(signed_headers)}; bh={bh}; b=")
    sig_header = b"DKIM-Signature:" + value.encode() + b"\r\n"
    available = {}
    for name, raw in headers:
        available.setdefault(name, []).append(raw)
    data = b"".join(dv._canon_header(available[n.encode()].pop(), head_mode) for n in signed_headers if available.get(n.encode()))
    data += dv._canon_header(sig_header, head_mode).rstrip(b"\r\n")
    if algo == "rsa-sha256":
        signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = key.sign(hashlib.sha256(data).digest())
    return b"DKIM-Signature:" + value.encode() + base64.b64encode(signature) + b"\r\n" + message


def resolver_for(records: dict[str, bytes]):
    return lambda name: records[name]


MESSAGE = (b"From: Calendly <notifications@calendly.com>\r\nTo: sales@us.example\r\nSubject: " + BOOKED_SUBJECT.encode()
           + b"\r\nDate: Tue, 27 Oct 2026 16:00:00 +0000\r\nMessage-ID: <n1@calendly.example>\r\n"
           + b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           + BOOKED_BODY.replace("\n", "\r\n").encode() + b"\r\n")


@pytest.mark.parametrize("canon", ["relaxed/relaxed", "simple/simple", "relaxed/simple", "simple/relaxed"])
def test_a_genuine_signature_verifies_in_every_canonical_form(canon):
    signed = sign(MESSAGE, "calendly.com", canon=canon)
    assert dv.verified_domains(signed, resolver_for({"s1._domainkey.calendly.com": dns_record(RSA_KEY)})) == {"calendly.com"}


def test_ed25519_signatures_verify_too():
    signed = sign(MESSAGE, "calendly.com", key=ED_KEY, selector="ed")
    assert dv.verified_domains(signed, resolver_for({"ed._domainkey.calendly.com": dns_record(ED_KEY)})) == {"calendly.com"}


def test_any_change_or_a_wrong_key_means_not_verified():
    records = {"s1._domainkey.calendly.com": dns_record(RSA_KEY)}
    signed = sign(MESSAGE, "calendly.com")
    assert dv.verified_domains(signed.replace(b"10:00am - 10:20am", b"11:00am - 11:20am"), resolver_for(records)) == set()
    assert dv.verified_domains(signed.replace(b"Subject: New Event", b"Subject: Canceled"), resolver_for(records)) == set()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert dv.verified_domains(signed, resolver_for({"s1._domainkey.calendly.com": dns_record(other)})) == set()
    assert dv.verified_domains(signed, resolver_for({"s1._domainkey.calendly.com": b"v=DKIM1; p="})) == set()  # revoked
    assert dv.verified_domains(MESSAGE, resolver_for(records)) == set()  # no signature at all
    no_from = sign(MESSAGE, "calendly.com", signed_headers=("to", "subject"))
    assert dv.verified_domains(no_from, resolver_for(records)) == set()  # From must be signed
    assert dv.auth_results(signed, resolver_for(records)) == "aime.dkim; dkim=pass header.d=calendly.com"


def notice_in_inbox(ctx, uid, raw, monkeypatch, records):
    monkeypatch.setattr(dv, "_public_key", lambda selector, domain, resolver=None: _key(records, selector, domain))
    FakeIMAP.messages[uid] = raw
    run_inbox(ctx)
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()


def _key(records, selector, domain):
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    tags = dv._tags(records[f"{selector}._domainkey.{domain}"])
    data = base64.b64decode(tags["p"])
    return ed25519.Ed25519PublicKey.from_public_bytes(data) if tags.get("k") == "ed25519" else load_der_public_key(data)


def test_the_inbox_applies_a_signed_calendly_notice_and_holds_a_forged_one(make_ctx, monkeypatch):
    ctx, _, _ = contacted(make_ctx, calendly_verified_only=True)
    FakeIMAP.messages = {}
    records = {"s1._domainkey.calendly.com": dns_record(RSA_KEY)}
    forged = MESSAGE.replace(b"<n1@calendly.example>", b"<forged@calendly.example>").replace(
        b"From: Calendly", b"Authentication-Results: mx.us.example; dkim=pass header.d=calendly.com\r\nFrom: Calendly")
    row = notice_in_inbox(ctx, 1, forged, monkeypatch, records)  # a pass the sender wrote: not believed
    assert row.status == "needs_review" and row.auth_results == "aime.dkim; dkim=none"
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).first() is None
    row = notice_in_inbox(ctx, 2, sign(MESSAGE, "calendly.com"), monkeypatch, records)
    assert row.status == "actioned" and row.auth_results == "aime.dkim; dkim=pass header.d=calendly.com"
    with ctx.db.session() as s:
        assert len(s.execute(select(Meeting)).scalars().all()) == 1


def test_a_server_line_counts_only_when_that_server_is_trusted(make_ctx):
    from aime.mail_auth import calendly_verified

    ctx = make_ctx(calendly_verified_only=True)
    line = "mx.us.example; dkim=pass header.d=calendly.com"
    with ctx.db.session() as s:
        assert calendly_verified(s, ctx.settings, "notifications@calendly.com", line)[0] is False
    trusted = make_ctx(calendly_verified_only=True, trusted_authserv_ids="mx.us.example")
    with trusted.db.session() as s:
        assert calendly_verified(s, trusted.settings, "notifications@calendly.com", line)[0] is True
