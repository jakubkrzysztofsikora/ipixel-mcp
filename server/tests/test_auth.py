import pytest

from ipixel_mcp import auth
from ipixel_mcp.auth import Unauthorized, authorize


STATIC = "s3cret-token"


def test_static_bearer_grants_non_admin():
    p = authorize({"Authorization": f"Bearer {STATIC}"}, static_token=STATIC)
    assert p.kind == "static"
    assert auth.SCOPE_DISPLAY in p.scopes
    assert auth.SCOPE_ADMIN not in p.scopes


def test_static_bearer_case_insensitive_header():
    p = authorize({"authorization": f"bearer {STATIC}"}, static_token=STATIC)
    assert p.kind == "static"


def test_wrong_token_is_unauthorized():
    with pytest.raises(Unauthorized):
        authorize({"Authorization": "Bearer nope"}, static_token=STATIC)


def test_no_credential_is_unauthorized():
    with pytest.raises(Unauthorized):
        authorize({}, static_token=STATIC)


def test_access_jwt_takes_precedence_and_reads_scopes():
    headers = {
        "Cf-Access-Jwt-Assertion": "valid-jwt",
        "X-Mcp-Scopes": "ipixel:display ipixel:admin",
        "Authorization": "Bearer nope",
    }
    p = authorize(headers, static_token=STATIC, access_jwt_verifier=lambda j: j == "valid-jwt")
    assert p.kind == "worker"
    assert auth.SCOPE_ADMIN in p.scopes
    p.require(auth.SCOPE_ADMIN)  # does not raise


def test_invalid_access_jwt_falls_back_then_401():
    headers = {"Cf-Access-Jwt-Assertion": "forged"}
    with pytest.raises(Unauthorized):
        authorize(headers, static_token=STATIC, access_jwt_verifier=lambda j: False)


def test_require_scope_enforced():
    p = authorize({"Authorization": f"Bearer {STATIC}"}, static_token=STATIC)
    with pytest.raises(Unauthorized):
        p.require(auth.SCOPE_ADMIN)
