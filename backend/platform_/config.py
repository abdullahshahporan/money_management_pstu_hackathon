"""Application configuration, validated once at startup.

Spec 21.4: secrets come from the environment, never from source control.
Configuration is validated eagerly so a misconfigured deployment fails at boot
with a clear message rather than at the first transfer.
"""

from __future__ import annotations

import socket
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    # Defaults to the machine/container hostname so each replica identifies
    # itself in logs and health responses without per-replica configuration.
    instance_id: str = Field(default_factory=socket.gethostname)
    log_level: str = "INFO"

    # -- database ----------------------------------------------------------
    database_url: str = "postgresql+psycopg://mm_app:mm_app_dev_pw@localhost:5432/moneymovement"
    migration_database_url: str = (
        "postgresql+psycopg://mm_owner:mm_owner_dev_pw@localhost:5432/moneymovement"
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: int = Field(default=10, ge=1)
    statement_timeout_ms: int = Field(default=10_000, ge=100)
    idle_in_transaction_timeout_ms: int = Field(default=15_000, ge=100)
    lock_timeout_ms: int = Field(default=5_000, ge=100)

    # Spec 11.3: bounded retry with jitter on deadlock / serialization failure.
    transaction_max_attempts: int = Field(default=3, ge=1, le=10)
    transaction_retry_base_delay_ms: int = Field(default=25, ge=1)

    # -- dependencies ------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    # Disabled in the test suite: throttling would make assertions depend on
    # how fast the suite happens to run.
    rate_limit_enabled: bool = True
    rabbitmq_url: str = "amqp://mm:mm_dev_pw@localhost:5672/"

    # -- security ----------------------------------------------------------
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = Field(default=900, ge=60)
    refresh_token_ttl_seconds: int = Field(default=604_800, ge=300)
    engineering_api_key: str = Field(min_length=8)

    # Spec 21.1: transaction PIN lockout.
    pin_max_attempts: int = Field(default=5, ge=1)
    pin_lockout_seconds: int = Field(default=900, ge=30)

    # -- money policy ------------------------------------------------------
    opening_balance_minor: int = Field(default=10_000_000, ge=0)  # BDT 100,000.00
    max_transfer_amount_minor: int = Field(default=5_000_000_000, ge=1)
    daily_transfer_limit_minor: int = Field(default=50_000_000, ge=1)  # BDT 500,000.00
    money_request_expiry_hours: int = Field(default=72, ge=1)
    safepay_auto_release_hours: int = Field(default=72, ge=1, le=168)
    overdraft_max_draw_minor: int = Field(default=50_000, ge=1)
    overdraft_lien_sweep_basis_points: int = Field(default=5_000, ge=1, le=10_000)

    # -- outbox ------------------------------------------------------------
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_max_attempts: int = Field(default=8, ge=1)

    @field_validator("jwt_secret")
    @classmethod
    def _reject_dev_secret_in_production(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("environment") == "production" and "dev-only" in value:
            raise ValueError("The development JWT secret must not be used in production")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def redacted(self) -> dict[str, Any]:
        """Config safe to log at startup. Spec 23.1: never log secrets."""
        secret_fields = {"jwt_secret", "engineering_api_key"}
        url_fields = {"database_url", "migration_database_url", "redis_url", "rabbitmq_url"}
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if name in secret_fields:
                out[name] = "***"
            elif name in url_fields and isinstance(value, str) and "@" in value:
                scheme, _, rest = value.partition("://")
                out[name] = f"{scheme}://***@{rest.rpartition('@')[2]}"
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
