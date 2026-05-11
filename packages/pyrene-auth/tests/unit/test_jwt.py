"""Unit tests for JWT encode/decode + access/refresh token factories.

Uses an injectable `now` parameter to make TTL assertions deterministic.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from pyrene_auth.jwt import (
    InvalidTokenError,
    JwtSettings,
    TokenPayload,
    decode_token,
    encode_token,
    make_access_token,
    make_refresh_token,
)

# 32+ byte secret to silence PyJWT InsecureKeyLengthWarning for HS256.
_TEST_SECRET = "test-secret-with-thirty-two-plus-bytes-padding-do-not-use-in-prod"


@pytest.fixture
def settings() -> JwtSettings:
    return JwtSettings(
        secret=_TEST_SECRET,
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )


def test_access_token_round_trip(settings: JwtSettings) -> None:
    user_id = uuid4()
    team_id = uuid4()
    now = int(time.time())
    token = make_access_token(
        user_id, team_id, ("analyst", "viewer"), settings, now=now
    )
    decoded = decode_token(token, settings)
    assert decoded.sub == user_id
    assert decoded.team_id == team_id
    assert decoded.roles == ("analyst", "viewer")
    assert decoded.type == "access"
    assert decoded.iat == now
    assert decoded.exp == now + 900


def test_refresh_token_omits_team_and_roles(settings: JwtSettings) -> None:
    user_id = uuid4()
    now = int(time.time())
    token = make_refresh_token(user_id, settings, now=now)
    decoded = decode_token(token, settings)
    assert decoded.sub == user_id
    assert decoded.team_id is None
    assert decoded.roles == ()
    assert decoded.type == "refresh"
    assert decoded.exp == now + 604800


def test_expired_token_rejected(settings: JwtSettings) -> None:
    # Issue at epoch=0 with 900s TTL → already expired by current time.
    token = make_access_token(uuid4(), uuid4(), (), settings, now=0)
    with pytest.raises(InvalidTokenError, match="expired"):
        decode_token(token, settings)


def test_tampered_signature_rejected(settings: JwtSettings) -> None:
    now = int(time.time())
    token = make_access_token(uuid4(), uuid4(), (), settings, now=now)
    # Flip the last char of the signature segment.
    parts = token.split(".")
    parts[-1] = parts[-1][:-1] + ("A" if parts[-1][-1] != "A" else "B")
    bad = ".".join(parts)
    with pytest.raises(InvalidTokenError):
        decode_token(bad, settings)


def test_wrong_secret_rejected(settings: JwtSettings) -> None:
    now = int(time.time())
    token = make_access_token(uuid4(), uuid4(), (), settings, now=now)
    other_settings = JwtSettings(secret="different-32-plus-byte-secret-aaaaaaaaaaaaaaa")
    with pytest.raises(InvalidTokenError):
        decode_token(token, other_settings)


def test_encode_decode_token_payload_directly(settings: JwtSettings) -> None:
    now = int(time.time())
    payload = TokenPayload(
        sub=uuid4(),
        team_id=uuid4(),
        roles=("admin",),
        iat=now,
        exp=now + 900,
        type="access",
    )
    token = encode_token(payload, settings)
    decoded = decode_token(token, settings)
    assert decoded == payload
