"""Password and PIN hashing (spec 21.1).

Argon2id is the current recommendation: memory-hard, so a GPU or ASIC farm
gains far less advantage than it does against a purely compute-bound hash.

Two separate secrets exist per user, and they are hashed with the same
primitive but serve different jobs (spec 21.1): the password proves *identity*
at login, the PIN authorises an *individual money movement*. Keeping them
separate means a stolen session token still cannot move money.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

__all__ = ["PasswordHasherService"]


class PasswordHasherService:
    """Wraps argon2-cffi with parameters chosen for this workload."""

    def __init__(
        self,
        *,
        time_cost: int = 2,
        memory_cost_kib: int = 64 * 1024,
        parallelism: int = 2,
    ) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, secret: str) -> str:
        """Return an encoded hash. The salt is generated per call, inside."""
        return self._hasher.hash(secret)

    def verify(self, hashed: str, secret: str) -> bool:
        """Constant-time verification. Returns False rather than raising.

        A wrong password is an expected outcome, not an exception - modelling
        it as a boolean keeps the caller from accidentally leaking *which*
        failure occurred through differing error paths.
        """
        try:
            self._hasher.verify(hashed, secret)
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return False
        return True

    def needs_rehash(self, hashed: str) -> bool:
        """True when the stored hash used weaker parameters than current policy.

        Lets cost parameters be raised over time and applied transparently on
        the user's next successful login.
        """
        try:
            return self._hasher.check_needs_rehash(hashed)
        except (InvalidHashError, ValueError):
            return True
