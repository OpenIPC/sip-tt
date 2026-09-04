"""HTTP Digest, both directions.

A test tool needs both halves. It answers challenges, because a DUT that
registers will challenge it; and it *issues* challenges, because the registrar
side has to prove the DUT computes a response correctly — and because "does
this device authenticate inbound INVITEs at all" is itself a test worth having
(majestic today does not, so anyone who can reach UDP/5060 can call it).

MD5 only, deliberately. RFC 8760's SHA-256 exists and almost nothing in the
embedded VoIP world speaks it; a test that offered it and accepted a device's
refusal would be measuring nothing. When a DUT does support it, this is the
file to widen.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from .message import split_params


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


@dataclass
class Challenge:
    """A parsed WWW-Authenticate / Proxy-Authenticate header."""

    realm: str = ""
    nonce: str = ""
    opaque: str = ""
    qop: str = ""
    algorithm: str = "MD5"
    stale: bool = False
    is_proxy: bool = False

    @classmethod
    def parse(cls, value: str, is_proxy: bool = False) -> "Challenge | None":
        if not value:
            return None
        scheme, _, rest = value.strip().partition(" ")
        if scheme.lower() != "digest":
            return None
        c = cls(is_proxy=is_proxy)
        for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', rest):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
            if key == "realm":
                c.realm = val
            elif key == "nonce":
                c.nonce = val
            elif key == "opaque":
                c.opaque = val
            elif key == "qop":
                c.qop = val
            elif key == "algorithm":
                c.algorithm = val
            elif key == "stale":
                c.stale = val.lower() == "true"
        return c

    def header_name(self) -> str:
        return "Proxy-Authorization" if self.is_proxy else "Authorization"


def respond(challenge: Challenge, *, username: str, password: str,
            method: str, uri: str, cnonce: str | None = None,
            nc: int = 1, body: str = "") -> str:
    """Build the Authorization header value answering `challenge`.

    Supports the RFC 2069 form (no qop) and the RFC 2617/7616 ``qop=auth``
    form, because devices in the field send both and a registrar picks.
    ``auth-int`` is computed if explicitly offered alone, since a device that
    asks for it and gets ``auth`` back will reject the retry.
    """
    ha1 = _md5(f"{username}:{challenge.realm}:{password}")

    qops = [q.strip().lower() for q in challenge.qop.split(",") if q.strip()]
    use_int = qops == ["auth-int"]
    ha2 = (_md5(f"{method}:{uri}:{_md5(body)}") if use_int
           else _md5(f"{method}:{uri}"))

    parts = [f'username="{username}"', f'realm="{challenge.realm}"',
             f'nonce="{challenge.nonce}"', f'uri="{uri}"']

    if qops:
        chosen = "auth-int" if use_int else "auth"
        cnonce = cnonce or os.urandom(8).hex()
        nc_s = f"{nc:08x}"
        digest = _md5(f"{ha1}:{challenge.nonce}:{nc_s}:{cnonce}:{chosen}:{ha2}")
        parts += [f'response="{digest}"', f"qop={chosen}", f"nc={nc_s}",
                  f'cnonce="{cnonce}"']
    else:
        digest = _md5(f"{ha1}:{challenge.nonce}:{ha2}")
        parts.append(f'response="{digest}"')

    parts.append(f"algorithm={challenge.algorithm}")
    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')
    return "Digest " + ", ".join(parts)


def make_challenge(realm: str, *, qop: str = "auth", nonce: str | None = None,
                   opaque: str = "", stale: bool = False) -> str:
    """Build a WWW-Authenticate value, for the registrar/UAS side."""
    nonce = nonce or os.urandom(16).hex()
    parts = [f'realm="{realm}"', f'nonce="{nonce}"']
    if qop:
        parts.append(f'qop="{qop}"')
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if stale:
        parts.append("stale=true")
    parts.append("algorithm=MD5")
    return "Digest " + ", ".join(parts)


def verify(auth_header: str, *, password: str, method: str,
           body: str = "") -> tuple[bool, str]:
    """Check a client's Authorization header. Returns (ok, why-not).

    The URI is taken from the *header*, not from the request line. They are
    supposed to match, and a mismatch is worth a test of its own, but the
    digest is defined over what the client claimed it was authenticating.
    """
    if not auth_header:
        return False, "no Authorization header"
    scheme, _, rest = auth_header.strip().partition(" ")
    if scheme.lower() != "digest":
        return False, f"scheme is {scheme!r}, not Digest"

    p: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', rest):
        p[m.group(1).lower()] = (m.group(2) if m.group(2) is not None
                                 else (m.group(3) or ""))

    for required in ("username", "realm", "nonce", "uri", "response"):
        if required not in p:
            return False, f"Authorization lacks {required}"

    ha1 = _md5(f"{p['username']}:{p['realm']}:{password}")
    if p.get("qop", "").lower() == "auth-int":
        ha2 = _md5(f"{method}:{p['uri']}:{_md5(body)}")
    else:
        ha2 = _md5(f"{method}:{p['uri']}")

    if p.get("qop"):
        for required in ("nc", "cnonce"):
            if required not in p:
                return False, f"qop={p['qop']} but no {required}"
        want = _md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:"
                    f"{p['qop']}:{ha2}")
    else:
        want = _md5(f"{ha1}:{p['nonce']}:{ha2}")

    if want != p["response"]:
        return False, "digest mismatch"
    return True, ""


def challenge_from(response_msg) -> Challenge | None:
    """Pull the challenge out of a 401 or 407, keeping which one it was.

    Answering a 407 with an ``Authorization`` header is a real and common bug
    — majestic does exactly this today — so the proxy-ness is carried on the
    Challenge rather than discarded here.
    """
    www = response_msg.headers.get("www-authenticate")
    if www:
        return Challenge.parse(www, is_proxy=False)
    proxy = response_msg.headers.get("proxy-authenticate")
    if proxy:
        return Challenge.parse(proxy, is_proxy=True)
    return None
