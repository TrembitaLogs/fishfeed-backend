"""Tests for password hashing utilities."""

from app.utils.password import hash_password, verify_password


def test_hash_password_returns_argon2_hash():
    """Test that hash_password returns a valid argon2id hash."""
    password = "test_password_123"
    hashed = hash_password(password)

    assert hashed.startswith("$argon2id$")
    assert "$m=65536" in hashed
    assert ",t=3" in hashed
    assert ",p=4$" in hashed


def test_verify_password_returns_true_for_correct_password():
    """Test that verify_password returns True for correct password."""
    password = "correct_password"
    hashed = hash_password(password)

    assert verify_password(password, hashed) is True


def test_verify_password_returns_false_for_incorrect_password():
    """Test that verify_password returns False for incorrect password."""
    password = "correct_password"
    hashed = hash_password(password)

    assert verify_password("wrong_password", hashed) is False


def test_different_passwords_have_different_hashes():
    """Test that different passwords produce different hashes (salt)."""
    password1 = "password_one"
    password2 = "password_two"

    hash1 = hash_password(password1)
    hash2 = hash_password(password2)

    assert hash1 != hash2


def test_same_password_produces_different_hashes():
    """Test that same password hashed twice produces different hashes due to salt."""
    password = "same_password"

    hash1 = hash_password(password)
    hash2 = hash_password(password)

    assert hash1 != hash2


def test_verify_password_with_empty_password():
    """Test verify_password with empty password."""
    hashed = hash_password("some_password")

    assert verify_password("", hashed) is False


def test_hash_password_with_special_characters():
    """Test hash_password handles special characters."""
    password = "p@$$w0rd!#%^&*()_+-=[]{}|;':\",./<>?"
    hashed = hash_password(password)

    assert hashed.startswith("$argon2id$")
    assert verify_password(password, hashed) is True


def test_hash_password_with_unicode():
    """Test hash_password handles unicode characters."""
    password = "пароль_密码_パスワード"
    hashed = hash_password(password)

    assert hashed.startswith("$argon2id$")
    assert verify_password(password, hashed) is True
