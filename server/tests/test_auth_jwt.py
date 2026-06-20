"""Offline tests for the Cloudflare Access JWT verifier.

We self-generate a small RSA key in pure Python (no PyJWT, no cryptography, no
network), sign a token with RS256 using the same EMSA-PKCS1-v1_5 construction
the verifier checks, and verify it via the injected JWKS fetcher.
"""

import base64
import hashlib
import json
import time

import pytest

from ipixel_mcp import auth
from ipixel_mcp.auth import make_access_jwt_verifier, pure_rs256_verify


# --- tiny RSA keygen (test-only, small key for speed) ------------------------

def _is_probable_prime(n, k=20):
    import random
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31):
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(k):
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits):
    import random
    while True:
        cand = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(cand):
            return cand


def _gen_rsa(bits=1024):
    e = 65537
    half = bits // 2
    while True:
        p = _gen_prime(half)
        q = _gen_prime(half)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        if n.bit_length() == bits:
            return n, e, d


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _int_to_b64url(i):
    return _b64url(i.to_bytes((i.bit_length() + 7) // 8, "big"))


def _sign_rs256(message, n, e, d):
    k = (n.bit_length() + 7) // 8
    digest = hashlib.sha256(message).digest()
    di_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t = di_prefix + digest
    ps = b"\xff" * (k - len(t) - 3)
    em = b"\x00\x01" + ps + b"\x00" + t
    sig_int = pow(int.from_bytes(em, "big"), d, n)
    return sig_int.to_bytes(k, "big")


def _make_token(claims, n, e, d, kid="k1"):
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(claims).encode())
    sig = _sign_rs256(f"{h}.{p}".encode(), n, e, d)
    return f"{h}.{p}.{_b64url(sig)}"


@pytest.fixture(scope="module")
def keypair():
    return _gen_rsa(1024)


@pytest.fixture
def jwks(keypair):
    n, e, d = keypair
    return {"keys": [{"kty": "RSA", "kid": "k1", "n": _int_to_b64url(n), "e": _int_to_b64url(e)}]}


TEAM = "myteam.cloudflareaccess.com"
AUD = "aud-tag-123"
NOW = 1_000_000.0


def _verifier(jwks, **kw):
    return make_access_jwt_verifier(
        TEAM, AUD, jwks_fetcher=lambda: jwks, clock=lambda: NOW, **kw
    )


def _claims(**over):
    c = {"iss": f"https://{TEAM}", "aud": AUD, "exp": NOW + 100, "nbf": NOW - 10}
    c.update(over)
    return c


def test_valid_token_accepted(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d)
    assert _verifier(jwks)(token) is True


def test_wrong_aud_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(aud="other"), n, e, d)
    assert _verifier(jwks)(token) is False


def test_wrong_iss_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(iss="https://evil.example"), n, e, d)
    assert _verifier(jwks)(token) is False


def test_expired_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(exp=NOW - 1000), n, e, d)
    assert _verifier(jwks)(token) is False


def test_tampered_signature_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d)
    h, p, s = token.split(".")
    bad = f"{h}.{p}.{s[:-2]}AA"
    assert _verifier(jwks)(bad) is False


def test_tampered_payload_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d)
    h, p, s = token.split(".")
    forged_payload = _b64url(json.dumps(_claims(aud="other")).encode())
    assert _verifier(jwks)(f"{h}.{forged_payload}.{s}") is False


def test_alg_none_downgrade_rejected(jwks):
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}).encode())
    payload = _b64url(json.dumps(_claims()).encode())
    assert _verifier(jwks)(f"{header}.{payload}.") is False


def test_unknown_kid_rejected(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d, kid="other-kid")
    assert _verifier(jwks)(token) is False


def test_malformed_token_rejected(jwks):
    v = _verifier(jwks)
    assert v("not-a-jwt") is False
    assert v("a.b") is False


def test_aud_list_membership(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(aud=["x", AUD, "y"]), n, e, d)
    assert _verifier(jwks)(token) is True


def test_injected_rsa_verifier_used(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d)
    calls = {"n": 0}

    def fake(msg, sig, jwk):
        calls["n"] += 1
        return pure_rs256_verify(msg, sig, jwk)

    assert _verifier(jwks, rsa_verifier=fake)(token) is True
    assert calls["n"] == 1


def test_authorize_integrates_with_verifier(keypair, jwks):
    n, e, d = keypair
    token = _make_token(_claims(), n, e, d)
    v = _verifier(jwks)
    p = auth.authorize(
        {"Cf-Access-Jwt-Assertion": token, "X-Mcp-Scopes": "ipixel:admin"},
        static_token="static",
        access_jwt_verifier=v,
    )
    assert p.kind == "worker"
    assert auth.SCOPE_ADMIN in p.scopes
