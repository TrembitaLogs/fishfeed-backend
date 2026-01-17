"""Password hashing utilities using argon2id algorithm."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
)


def hash_password(password: str) -> str:
    """Hash a password using argon2id algorithm.

    Args:
        password: Plain text password to hash.

    Returns:
        Argon2id hash string.
    """
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against an argon2id hash.

    Args:
        password: Plain text password to verify.
        hashed: Argon2id hash to verify against.

    Returns:
        True if password matches, False otherwise.
    """
    try:
        _hasher.verify(hashed, password)
        return True
    except VerifyMismatchError:
        return False
