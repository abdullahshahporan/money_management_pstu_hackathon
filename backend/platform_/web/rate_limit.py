"""Distributed rate limiting (spec 17.6, 14.4).

Counters live in Redis, not in process memory. With several API replicas behind
a load balancer, an in-memory limiter would let each replica grant the full
quota independently - a "10 per minute" rule silently becomes 10 x replicas
(spec 10). Redis gives one shared view.

Redis is *not* authoritative for money, and this module is careful about what
happens when it is unavailable (spec 14.4):

*   high-risk endpoints - login, transfers - **fail closed**. Losing the
    ability to throttle a credential-stuffing or drain attempt is worse than
    briefly refusing traffic.
*   safe read endpoints **fail open**, so a Redis blip does not take down the
    ability to check a balance.

Either way the ledger is untouched: PostgreSQL still enforces every financial
invariant, so a Redis outage degrades protection, never correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import redis
from redis.exceptions import RedisError

from platform_.kernel.errors import RateLimitedError

logger = logging.getLogger(__name__)

__all__ = ["RateLimitPolicy", "RateLimiter", "RedisRateLimiter"]


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A quota. ``fail_closed`` decides behaviour when Redis is unreachable."""

    name: str
    limit: int
    window_seconds: int
    fail_closed: bool = False


# Spec 17.6. Illustrative values, to be tuned by load testing rather than guessed.
POLICIES = {
    "login": RateLimitPolicy("login", limit=5, window_seconds=60, fail_closed=True),
    "register": RateLimitPolicy("register", limit=5, window_seconds=300, fail_closed=True),
    "transfer": RateLimitPolicy("transfer", limit=10, window_seconds=60, fail_closed=True),
    "money_request": RateLimitPolicy("money_request", limit=20, window_seconds=60),
    "lookup": RateLimitPolicy("lookup", limit=60, window_seconds=60),
    "read": RateLimitPolicy("read", limit=120, window_seconds=60),
}

# A fixed window per key. Chosen over a sliding log for cost: one INCR and one
# EXPIRE per request rather than a sorted set per key. The known trade-off is
# that a burst straddling a window boundary can briefly reach 2x the limit;
# for abuse protection - as opposed to billing - that is acceptable, and it is
# stated here rather than left for someone to discover.
_LUA_FIXED_WINDOW = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


class RateLimiter:
    """Port. Raises RateLimitedError when a quota is exhausted."""

    def check(self, *, policy_name: str, identifier: str) -> None:  # pragma: no cover
        raise NotImplementedError


class RedisRateLimiter(RateLimiter):
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(
            url,
            socket_timeout=0.25,
            socket_connect_timeout=0.25,
            retry_on_timeout=False,
            decode_responses=True,
        )
        self._script = self._client.register_script(_LUA_FIXED_WINDOW)

    def check(self, *, policy_name: str, identifier: str) -> None:
        policy = POLICIES.get(policy_name)
        if policy is None:
            return

        key = f"ratelimit:{policy.name}:{identifier}"
        try:
            # A Lua script so INCR and EXPIRE are one atomic round trip. Doing
            # them separately risks a key that never expires if the process
            # dies in between, permanently locking that identifier out.
            current, ttl = self._script(keys=[key], args=[policy.window_seconds])
        except RedisError:
            logger.warning(
                "rate_limiter_unavailable",
                extra={"policy": policy.name, "fail_closed": policy.fail_closed},
            )
            if policy.fail_closed:
                raise RateLimitedError(
                    retry_after_seconds=5,
                    message="Rate limiting is temporarily unavailable. Please retry shortly.",
                ) from None
            return

        if int(current) > policy.limit:
            raise RateLimitedError(
                retry_after_seconds=max(int(ttl), 1),
                details={"limit": policy.limit, "windowSeconds": policy.window_seconds},
            )

    def close(self) -> None:
        try:
            self._client.close()
        except RedisError:
            logger.warning("redis_close_failed", exc_info=True)


class NoopRateLimiter(RateLimiter):
    """Used in tests, where throttling would only make assertions flaky.

    The arguments are unused by design - this implementation exists to satisfy
    the port while doing nothing.
    """

    def check(self, *, policy_name: str, identifier: str) -> None:  # noqa: ARG002
        return
