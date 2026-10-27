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
