"""Regression tests for the deterministic demo reset."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import create_app
from scripts.seed import reset


@pytest.mark.integration
def test_seed_reset_removes_spot_me_owned_account() -> None:
    """A user-owned pool must not block deletion of its sponsoring user."""
    with TestClient(create_app()) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "phone": "01712999999",
                "displayName": "Seed Sponsor",
                "password": "demo-password-2026",
                "pin": "1234",
            },
        )
        assert registered.status_code == 201, registered.text
        token = registered.json()["data"]["accessToken"]

        pool = client.post(
            "/api/v1/overdraft/pools",
            json={"amountMinor": 50_000, "pin": "1234", "currency": "BDT"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "seed-reset-pool-001",
            },
        )
        assert pool.status_code == 201, pool.text

        container = client.app.state.container
        reset(container)

        with container.session_factory() as session:
            assert session.scalar(text("SELECT count(*) FROM users")) == 0
            assert (
                session.scalar(text("SELECT count(*) FROM accounts WHERE user_id IS NOT NULL"))
                == 0
            )
            assert session.scalar(text("SELECT count(*) FROM overdraft_pools")) == 0
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM accounts "
                        "WHERE balance_minor <> 0 OR version <> 0"
                    )
                )
                == 0
            )
