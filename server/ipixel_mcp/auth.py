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

import hmac
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

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
