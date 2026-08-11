import hashlib
import hmac

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_WEAK_PASSWORDS = frozenset(
    {
        "password",
        "password!2026aaaa",
        "qwerty123",
        "12345678",
        "admin123",
    }
)


def _peppered_password(plain: str, pepper: bytes) -> bytes:
    return hmac.digest(pepper, plain.encode("utf-8"), hashlib.sha256)


def hash_password(plain: str, *, pepper: bytes) -> str:
    """Hash a password with Argon2id after applying the deployment pepper."""
    return _PASSWORD_HASHER.hash(_peppered_password(plain, pepper))


def verify_password(plain: str, hashed: str, *, pepper: bytes) -> bool:
    """Return False for a mismatch or malformed encoded hash."""
    try:
        return _PASSWORD_HASHER.verify(hashed, _peppered_password(plain, pepper))
    except (InvalidHashError, VerificationError):
        return False


def validate_password_floor(plain: str, *, context: list[str]) -> list[str]:
    """Return stable policy violation codes for the platform password floor."""
    violations: list[str] = []
    if not 15 <= len(plain) <= 64:
        violations.append("length")
    if not any(character.isupper() for character in plain):
        violations.append("uppercase")
    if not any(character.islower() for character in plain):
        violations.append("lowercase")
    if not any(not character.isalnum() for character in plain):
        violations.append("special")

    normalized = plain.casefold()
    if normalized in _WEAK_PASSWORDS:
        violations.append("weak")
    if any(value.strip().casefold() in normalized for value in context if value.strip()):
        violations.append("context")
    return violations
