import pytest

from app.oauth_manual import extract_account_id
from app.token_utils import JWT_CLAIM_PATH, extract_account_id_from_token


@pytest.mark.parametrize(
    "token",
    ["", "opaque-token", "a.b", "a.b.c.d", "a.b.c", "a._w.c", "a.bm90LWpzb24.c", None],
)
def test_malformed_tokens_have_no_account_id(token):
    assert extract_account_id_from_token(token) is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {JWT_CLAIM_PATH: []},
        {JWT_CLAIM_PATH: {"chatgpt_account_id": False}},
        {JWT_CLAIM_PATH: {"chatgpt_account_id": ""}},
    ],
)
def test_invalid_claim_shapes_have_no_account_id(make_jwt, payload):
    assert extract_account_id_from_token(make_jwt(payload)) is None


def test_account_claim_and_oauth_response_precedence(make_jwt):
    token = make_jwt({JWT_CLAIM_PATH: {"chatgpt_account_id": "claim-account"}})
    assert extract_account_id_from_token(token) == "claim-account"
    assert extract_account_id(token, {}) == "claim-account"
    assert (
        extract_account_id(token, {"account_id": "response-account"})
        == "response-account"
    )
    assert extract_account_id(token, {"account_id": 42}) == "claim-account"
    assert extract_account_id("opaque-token", {"sub": "user-subject"}) is None


def test_optional_identity_and_display_order(make_jwt):
    from app.token_utils import account_display_name, extract_identity

    identity = extract_identity(
        {"id_token": make_jwt({"email": "user@example.com", "name": "Display Name"})}
    )
    assert identity == {
        "upstream_email": "user@example.com",
        "upstream_name": "Display Name",
    }
    assert extract_identity({"access_token": "opaque"}) == {
        "upstream_email": None,
        "upstream_name": None,
    }
    assert account_display_name("remark", "email", "name", "id", 1) == "remark"
    assert account_display_name("  ", "email", "name", "id", 1) == "email"
    assert account_display_name(None, None, "name", "id", 1) == "name"
    assert account_display_name(None, None, None, "id", 1) == "id"
    assert account_display_name(None, None, None, None, 1) == "账户 #1"
