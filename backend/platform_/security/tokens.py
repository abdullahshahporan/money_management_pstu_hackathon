"""Access and refresh token handling (spec 21.1).

Access tokens are short-lived signed JWTs, verified statelessly so any API
replica can serve any request (spec 17.1). Refresh tokens are long-lived and
therefore *not* self-contained: only their SHA-256 hash is stored, and they are
rotated on each use, so a leaked refresh token can be detected and revoked.

The asymmetry is deliberate. A stateless access token cannot be revoked before
it expires, which is why its lifetime is minutes; anything needing genuine
revocation goes through the database-backed session instead.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

__all__ = ["AccessTokenClaims", "TokenService", "hash_refresh_token"]


class TokenError(Exception):
    """Raised when a token is malformed, expired, or fails signature checks."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: str
    session_id: str
    issued_at: datetime
    expires_at: datetime


def hash_refresh_token(raw_token: str) -> str:
    """SHA-256 of the raw token.

    A plain hash, not Argon2: the token is 256 bits of cryptographic
    randomness, so it has no guessable structure for an attacker to brute
    force. Password hashing exists to slow down guessing of *low-entropy*
    secrets; applying it here would cost latency on every refresh for no gain.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class TokenService:
    def __init__(
        self,
        *,
        secret: str,
        algorithm: str = "HS256",
        access_ttl_seconds: int = 900,
        refresh_ttl_seconds: int = 604_800,
        issuer: str = "money-movement",
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._access_ttl = timedelta(seconds=access_ttl_seconds)
        self._refresh_ttl = timedelta(seconds=refresh_ttl_seconds)
        self._issuer = issuer

    def issue_access_token(self, *, user_id: str, session_id: str, now: datetime) -> str:
        expires_at = now + self._access_ttl
        payload = {
            "sub": user_id,
            "sid": session_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
            "typ": "access",
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def verify_access_token(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenError(str(exc)) from exc

        if payload.get("typ") != "access":
            # Without this, a refresh token could be presented as an access
            # token and would validate on signature alone.
            raise TokenError("Token is not an access token")

        return AccessTokenClaims(
            user_id=payload["sub"],
            session_id=payload.get("sid", ""),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    def issue_refresh_token(self) -> tuple[str, str]:
        """Return ``(raw_token, token_hash)``. Only the hash is ever stored."""
        raw = secrets.token_urlsafe(32)
        return raw, hash_refresh_token(raw)

    def refresh_expiry(self, now: datetime) -> datetime:
        return now + self._refresh_ttl
