"""Password hashing — argon2id via argon2-cffi.

OWASP-recommended; defeats GPU/ASIC offline cracking and provides a
timing-safe verify built-in. Cost parameters use the argon2-cffi defaults
(time_cost=3, memory_cost=64 MiB, parallelism=4), which match the OWASP
Argon2id baseline.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plaintext password (returns the argon2 encoded form)."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verification.

    Returns False on any mismatch / malformed hash without leaking which.
    `argon2-cffi.PasswordHasher.verify` is documented as timing-safe for
    legitimate hashes; malformed-hash short-circuits, but the caller in
    `routes/auth.py` always feeds a real hash from DB (or a sentinel hash on
    user-enumeration paths), so the timing characteristics are uniform.
    """
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


__all__ = ["hash_password", "verify_password"]
