"""Tests for JWT token utilities."""

from datetime import timedelta
from uuid import uuid4

from jose import jwt

from app.config import get_settings
from app.utils.jwt import create_access_token, create_refresh_token, decode_token


def test_create_access_token_returns_valid_jwt():
    """Test that create_access_token generates a valid JWT."""
    user_id = uuid4()
    token = create_access_token(user_id)

    assert isinstance(token, str)
    assert len(token.split(".")) == 3


def test_create_access_token_has_correct_payload():
    """Test that access token payload contains correct claims."""
    user_id = uuid4()
    token = create_access_token(user_id)

    payload = decode_token(token)

    assert payload is not None
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"
    assert "exp" in payload
    assert "iat" in payload


def test_create_refresh_token_returns_valid_jwt():
    """Test that create_refresh_token generates a valid JWT and jti."""
    user_id = uuid4()
    token, jti = create_refresh_token(user_id)

    assert isinstance(token, str)
    assert len(token.split(".")) == 3
    assert isinstance(jti, str)
    assert len(jti) == 36  # UUID string length


def test_create_refresh_token_has_type_refresh():
    """Test that refresh token has type='refresh' and jti in payload."""
    user_id = uuid4()
    token, jti = create_refresh_token(user_id)

    payload = decode_token(token)

    assert payload is not None
    assert payload["type"] == "refresh"
    assert payload["jti"] == jti


def test_decode_token_returns_correct_payload():
    """Test that decode_token returns the correct payload."""
    user_id = uuid4()
    token = create_access_token(user_id)

    payload = decode_token(token)

    assert payload is not None
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"


def test_decode_token_returns_none_for_expired_token():
    """Test that decode_token returns None for expired token."""
    user_id = uuid4()
    token = create_access_token(user_id, expires_delta=timedelta(seconds=-1))

    payload = decode_token(token)

    assert payload is None


def test_decode_token_returns_none_for_invalid_token():
    """Test that decode_token returns None for invalid token."""
    payload = decode_token("invalid.token.here")

    assert payload is None


def test_decode_token_returns_none_for_tampered_token():
    """Test that decode_token returns None for tampered token."""
    user_id = uuid4()
    token = create_access_token(user_id)
    tampered_token = token[:-5] + "xxxxx"

    payload = decode_token(tampered_token)

    assert payload is None


def test_decode_token_returns_none_for_wrong_secret():
    """Test that decode_token returns None for token signed with wrong secret."""
    user_id = uuid4()
    settings = get_settings()

    fake_token = jwt.encode(
        {"sub": str(user_id), "type": "access"},
        "wrong-secret-key",
        algorithm=settings.JWT_ALGORITHM,
    )

    payload = decode_token(fake_token)

    assert payload is None


def test_access_and_refresh_tokens_are_different():
    """Test that access and refresh tokens for same user are different."""
    user_id = uuid4()

    access_token = create_access_token(user_id)
    refresh_token, _ = create_refresh_token(user_id)

    assert access_token != refresh_token


def test_custom_expires_delta_is_respected():
    """Test that custom expires_delta is applied to token."""
    user_id = uuid4()
    custom_delta = timedelta(hours=2)

    token = create_access_token(user_id, expires_delta=custom_delta)
    payload = decode_token(token)

    assert payload is not None
    expected_exp = payload["iat"] + int(custom_delta.total_seconds())
    assert abs(payload["exp"] - expected_exp) <= 1
