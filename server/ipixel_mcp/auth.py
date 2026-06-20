"""Origin authorization: single function, explicit precedence (review C-4 / §5).

The origin trusts exactly two callers and nothing else:

1. The Cloudflare Worker, proven by a verified **Cloudflare Access service-token
   JWT**. On this path the origin reads a trusted ``X-Mcp-Scopes`` header for
   scope gating. The origin NEVER validates the end-user's claude.ai OAuth token
   (that token's audience is the Worker, not us — no token passthrough).
2. Claude Code on the tailnet, proven by a **static bearer**. Fixed non-admin
   scope set. This endpoint advertises NO OAuth (Claude Code issue #59467), so a
   failure here is a plain 401 with no ``WWW-Authenticate``.

Stdlib-only; the Access-JWT verifier is injected so this is testable offline.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("ipixel_mcp.auth")

# Scope names used to gate tools. Admin gates destructive ops (clear/delete).
SCOPE_DISPLAY = "ipixel:display"
SCOPE_NOTIFY = "ipixel:notify"
SCOPE_GALLERY = "ipixel:gallery"
SCOPE_ADMIN = "ipixel:admin"

# Scopes granted to the direct static-bearer (Claude Code) path. No admin.
STATIC_BEARER_SCOPES = frozenset({SCOPE_DISPLAY, SCOPE_NOTIFY, SCOPE_GALLERY})


class Unauthorized(Exception):
    """Raised when no trusted caller can be established → plain 401."""


@dataclass(frozen=True)
class Principal:
    kind: str  # "worker" | "static"
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Unauthorized(f"missing required scope: {scope}")


# A verifier takes the raw Access JWT assertion and returns True if valid.
AccessJwtVerifier = Callable[[str], bool]


def _get(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup."""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def _parse_scopes(raw: Optional[str]) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split() if s.strip())


def _bearer_eq(header_value: str, expected_token: str) -> bool:
    prefix = "bearer "
    if not header_value.lower().startswith(prefix):
        return False
    presented = header_value[len(prefix):].strip()
    # constant-time compare to avoid token-length/﻿content timing leaks
    return hmac.compare_digest(presented, expected_token)


def authorize(
    headers: Mapping[str, str],
    *,
    static_token: Optional[str],
    access_jwt_verifier: Optional[AccessJwtVerifier] = None,
    worker_scope_header: str = "X-Mcp-Scopes",
    access_jwt_header: str = "Cf-Access-Jwt-Assertion",
) -> Principal:
    """Resolve the caller to a Principal or raise Unauthorized.

    Precedence: verified Access JWT (Worker) → static bearer → 401.
    """
    # 1) Cloudflare Worker via a *verified* Access service-token JWT.
    jwt = _get(headers, access_jwt_header)
    if jwt and access_jwt_verifier is not None and access_jwt_verifier(jwt):
        scopes = _parse_scopes(_get(headers, worker_scope_header))
        return Principal(kind="worker", scopes=scopes)

    # 2) Direct tailnet static bearer (Claude Code).
    auth = _get(headers, "Authorization")
    if auth and static_token and _bearer_eq(auth, static_token):
        return Principal(kind="static", scopes=STATIC_BEARER_SCOPES)

    raise Unauthorized("no trusted credential presented")


# =============================================================================
# Cloudflare Access service-token JWT verification (review C-4 / §5)
# =============================================================================
#
# The origin authenticates *the Worker* via the Cloudflare Access service-token
# JWT (header ``Cf-Access-Jwt-Assertion``). We verify:
#   - RS256 signature against the team's JWKS (kid-matched),
#   - ``aud`` contains the configured Access application AUD tag,
#   - ``iss`` equals ``https://<team>.cloudflareaccess.com``,
#   - ``exp`` (and ``nbf``/``iat`` if present) within a small clock skew.
#
# JWKS fetching and the RSA verify primitive are both injectable so a unit test
# can verify a self-signed token offline and without PyJWT. A pure-Python RS256
# verifier is included as the default crypto so tests need no third-party libs.

# (jwt_str) -> bool. This is the public verifier shape used by ``authorize``.
# (already defined above as AccessJwtVerifier)

# A JWKS fetcher returns the parsed JWKS dict ({"keys": [...]}). Injected so
# tests pass a static key set with no network. The default fetches over HTTPS.
JwksFetcher = Callable[[], Mapping[str, Any]]

# An RSA verify primitive: (message_bytes, signature_bytes, jwk_dict) -> bool.
# Injected so tests can use a tiny verifier (or the pure default) without PyJWT.
RsaVerifier = Callable[[bytes, bytes, Mapping[str, Any]], bool]

_DEFAULT_LEEWAY = 60.0  # seconds of clock skew tolerance


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def pure_rs256_verify(message: bytes, signature: bytes, jwk: Mapping[str, Any]) -> bool:
    """Pure-Python RS256 (RSASSA-PKCS1-v1_5 + SHA-256) verify, stdlib only.

    Used as the default crypto so tests (and a PyJWT-less deployment) can verify
    without third-party packages. Reconstructs the EMSA-PKCS1-v1_5 encoding and
    compares against ``signature ** e mod n``.
    """
    try:
        n = _b64url_uint(jwk["n"])
        e = _b64url_uint(jwk["e"])
    except (KeyError, ValueError):
        return False
    if not signature:
        return False

    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    # RSAVP1: m = s^e mod n
    m_int = pow(sig_int, e, n)
    em = m_int.to_bytes(k, "big")

    # EMSA-PKCS1-v1_5 for SHA-256:
    #   0x00 0x01 PS(0xFF...) 0x00 DigestInfo
    digest = hashlib.sha256(message).digest()
    # DER DigestInfo prefix for SHA-256.
    di_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t = di_prefix + digest
    ps_len = k - len(t) - 3
    if ps_len < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _select_jwk(jwks: Mapping[str, Any], kid: Optional[str]) -> Optional[Mapping[str, Any]]:
    keys = jwks.get("keys") or []
    if kid is not None:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    # No kid in the header: only safe if exactly one key is published.
    return keys[0] if len(keys) == 1 else None


def _https_jwks_fetcher(team_domain: str) -> JwksFetcher:
    """Default JWKS fetcher over HTTPS (lazy import; not used by tests)."""
    url = f"https://{team_domain}/cdn-cgi/access/certs"

    def _fetch() -> Mapping[str, Any]:
        import urllib.request  # lazy

        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed https URL
            return json.loads(resp.read().decode("utf-8"))

    return _fetch


def make_access_jwt_verifier(
    team_domain: str,
    aud: str,
    *,
    jwks_fetcher: Optional[JwksFetcher] = None,
    rsa_verifier: RsaVerifier = pure_rs256_verify,
    clock: Callable[[], float] = time.time,
    leeway: float = _DEFAULT_LEEWAY,
    jwks_cache_ttl: float = 600.0,
) -> AccessJwtVerifier:
    """Build a verifier callable for Cloudflare Access service-token JWTs.

    Validates the RS256 signature (against the team JWKS), ``aud``, ``iss`` and
    ``exp``/``nbf``. ``jwks_fetcher`` and ``rsa_verifier`` are injectable so a
    test can verify a self-generated key offline without PyJWT/network.

    ``team_domain`` is e.g. ``myteam.cloudflareaccess.com``; ``aud`` is the
    Access application's AUD tag.
    """
    issuer = f"https://{team_domain}"
    if jwks_fetcher is None:
        jwks_fetcher = _https_jwks_fetcher(team_domain)

    cache: dict[str, Any] = {"jwks": None, "at": 0.0}

    def _get_jwks(force: bool = False) -> Mapping[str, Any]:
        now = clock()
        if force or cache["jwks"] is None or (now - cache["at"]) > jwks_cache_ttl:
            try:
                cache["jwks"] = jwks_fetcher()
                cache["at"] = now
            except Exception as exc:  # noqa: BLE001
                logger.warning("JWKS fetch failed: %r", exc)
                if cache["jwks"] is None:
                    raise
        return cache["jwks"]

    def verify(token: str) -> bool:
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(sig_b64)
        except (ValueError, json.JSONDecodeError):
            return False

        if header.get("alg") != "RS256":
            return False  # only RS256; never accept "none" or HS* downgrade
        kid = header.get("kid")

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

        # Try the cached JWKS; on a kid miss, refresh once (key rotation). A JWKS
        # fetch failure (endpoint down/unreachable) must fail CLOSED — return
        # False so the caller gets a clean 401, not a 500 (PR review).
        try:
            jwk = _select_jwk(_get_jwks(), kid)
            if jwk is None:
                jwk = _select_jwk(_get_jwks(force=True), kid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("JWKS unavailable, rejecting token: %r", exc)
            return False
        if jwk is None:
            return False
        try:
            if not rsa_verifier(signing_input, signature, jwk):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("RSA verify error: %r", exc)
            return False

        # Claims.
        if payload.get("iss") != issuer:
            return False
        token_aud = payload.get("aud")
        auds = token_aud if isinstance(token_aud, list) else [token_aud]
        if aud not in auds:
            return False
        now = clock()
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or now > exp + leeway:
            return False
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now < nbf - leeway:
            return False
        return True

    return verify
