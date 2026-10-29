"""DKIM signature verification (RFC 6376), done by AIME itself on the raw email it reads, for mail servers that do not
stamp an Authentication-Results header (ours stamps none). A domain counts as verified only when its signature's
body hash matches and its RSA or Ed25519 signature checks out against the public key published in that domain's DNS.
Any doubt (no signature, an unknown algorithm, a DNS failure, a header or body changed in transit) means "not
verified", and a person confirms the email instead. Uses cryptography and dnspython, already required by AIME."""

from __future__ import annotations

import base64
import hashlib
import re

_WSP = re.compile(rb"[ \t]+")


def _split(raw: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """Headers as (name, the raw header including folded lines and CRLF), and the body."""
    raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        head, body = raw, b""
    headers: list[tuple[bytes, bytes]] = []
    for line in head.split(b"\r\n"):
        if line[:1] in (b" ", b"\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + line + b"\r\n")
        elif b":" in line:
            headers.append((line.split(b":", 1)[0].strip().lower(), line + b"\r\n"))
    return headers, body


def _tags(value: bytes) -> dict[str, str]:
    out = {}
    for part in re.sub(rb"\s+", b"", value).split(b";"):
        if b"=" in part:
            k, v = part.split(b"=", 1)
            out[k.decode("ascii", "replace")] = v.decode("ascii", "replace")
    return out


def _canon_header(raw_header: bytes, mode: str) -> bytes:
    if mode == "simple":
        return raw_header
    name, value = raw_header.split(b":", 1)
    value = _WSP.sub(b" ", value.replace(b"\r\n", b"")).strip()
    return name.strip().lower() + b":" + value + b"\r\n"


def _canon_body(body: bytes, mode: str) -> bytes:
    if mode == "relaxed":
        lines = [_WSP.sub(b" ", line).rstrip(b" \t") for line in body.split(b"\r\n")]
        body = b"\r\n".join(lines)
    while body.endswith(b"\r\n\r\n"):
        body = body[:-2]
    if mode == "simple":
        return body if body.endswith(b"\r\n") else body + b"\r\n"
    body = body.rstrip(b"\r\n")
    return body + b"\r\n" if body else b""


def _public_key(selector: str, domain: str, resolver=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    if resolver is None:
        import dns.resolver

        answer = dns.resolver.resolve(f"{selector}._domainkey.{domain}", "TXT", lifetime=5)
        txt = b"".join(b"".join(r.strings) for r in answer)
    else:
        txt = resolver(f"{selector}._domainkey.{domain}")
    tags = _tags(txt)
    data = base64.b64decode(tags.get("p", ""))
    if not data:
        return None  # an empty p= means the key was revoked
    if tags.get("k", "rsa") == "ed25519":
        return Ed25519PublicKey.from_public_bytes(data)
    return load_der_public_key(data)


def verify_one(raw: bytes, index: int = 0, resolver=None) -> str | None:
    """The signing domain if the index-th DKIM-Signature verifies, else None."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    headers, body = _split(raw)
    sigs = [h for h in headers if h[0] == b"dkim-signature"]
    if index >= len(sigs):
        return None
    sig_raw = sigs[index][1]
    tags = _tags(sig_raw.split(b":", 1)[1])
    algo, domain, selector = tags.get("a", ""), tags.get("d", "").lower(), tags.get("s", "")
    if algo not in ("rsa-sha256", "ed25519-sha256") or not domain or not selector or tags.get("v", "1") != "1":
        return None
    head_mode, _, body_mode = tags.get("c", "simple/simple").partition("/")
    body_mode = body_mode or "simple"
    if head_mode not in ("simple", "relaxed") or body_mode not in ("simple", "relaxed"):
        return None
    canon = _canon_body(body, body_mode)
    if "l" in tags:
        canon = canon[:int(tags["l"])]
    if base64.b64encode(hashlib.sha256(canon).digest()).decode() != tags.get("bh", ""):
        return None  # the body changed after signing
    signed = [n.strip().lower().encode() for n in tags.get("h", "").split(":") if n.strip()]
    if b"from" not in signed:
        return None
    available: dict[bytes, list[bytes]] = {}
    for name, raw_header in headers:
        available.setdefault(name, []).append(raw_header)
    data = b""
    for name in signed:  # each name takes the next occurrence from the bottom
        if available.get(name):
            data += _canon_header(available[name].pop(), head_mode)
    name_part, value_part = sig_raw.split(b":", 1)  # the signature header itself, its b= value left empty
    value_part = re.sub(rb"(^|;)(\s*b\s*=)[^;]*", rb"\1\2", value_part, count=1)
    unsigned = name_part + b":" + value_part
    data += _canon_header(unsigned, head_mode).rstrip(b"\r\n")
    try:
        key = _public_key(selector, domain, resolver)
        signature = base64.b64decode(tags.get("b", ""))
        if isinstance(key, RSAPublicKey):
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(key, Ed25519PublicKey) and algo == "ed25519-sha256":
            key.verify(signature, hashlib.sha256(data).digest())
        else:
            return None
    except (InvalidSignature, Exception):  # any doubt (bad key, DNS failure, malformed data): not verified
        return None
    return domain


def verified_domains(raw: bytes, resolver=None, limit: int = 4) -> set[str]:
    """Every domain whose DKIM signature on this email verifies (at most `limit` signatures are tried)."""
    out = set()
    count = len([1 for name, _ in _split(raw)[0] if name == b"dkim-signature"])
    for i in range(min(count, limit)):
        if domain := verify_one(raw, i, resolver):
            out.add(domain)
    return out


def auth_results(raw: bytes, resolver=None) -> str:
    """AIME's own Authentication-Results line for this email (authserv-id "aime.dkim")."""
    domains = sorted(verified_domains(raw, resolver))
    return "aime.dkim; " + ("; ".join(f"dkim=pass header.d={d}" for d in domains) if domains else "dkim=none")
