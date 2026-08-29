"""Audit-trail creation and database-enforced immutability."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from apps.api.main import create_app

pytestmark = pytest.mark.integration


def _key() -> str:
    return str(uuid.uuid4())


def _register(client: TestClient, phone: str, name: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "phone": phone,
            "displayName": name,
            "password": "correct-horse-99",
            "pin": "1234",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_successful_transfer_creates_a_correlated_audit_row() -> None:
    with TestClient(create_app()) as client:
        alice = _register(client, "01719000001", "Audit Alice")
        _register(client, "01719000002", "Audit Bob")

        response = client.post(
            "/api/v1/transfers",
            json={
                "recipientPhone": "01719000002",
                "amountMinor": 12_500,
                "pin": "1234",
                "note": "Audit proof",
            },
            headers={
                "Authorization": f"Bearer {alice['accessToken']}",
                "Idempotency-Key": _key(),
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()

        with client.app.state.container.session_factory() as session:
            row = (
                session.execute(
                    text(
                        "SELECT actor_user_id, action, resource_type, resource_id, "
                        "request_id, metadata "
                        "FROM audit_logs WHERE request_id = :request_id "
                        "AND action = 'TRANSFER_SUCCEEDED'"
                    ),
                    {"request_id": payload["meta"]["requestId"]},
                )
                .mappings()
                .one()
            )

        assert row["actor_user_id"] == alice["userId"]
        assert row["resource_type"] == "transfer"
        assert row["resource_id"] == payload["data"]["transferId"]
        assert row["request_id"] == payload["meta"]["requestId"]
        assert row["metadata"]["reference"] == payload["data"]["reference"]
        assert row["metadata"]["amountMinor"] == 12_500


def test_runtime_role_cannot_rewrite_or_delete_audit_history(engine: Engine) -> None:
    with engine.connect() as connection:
        privileges = (
            connection.execute(
                text(
                    "SELECT current_user AS role, "
                    "has_table_privilege(current_user, 'audit_logs', 'SELECT') AS can_select, "
                    "has_table_privilege(current_user, 'audit_logs', 'INSERT') AS can_insert, "
                    "has_table_privilege(current_user, 'audit_logs', 'UPDATE') AS can_update, "
                    "has_table_privilege(current_user, 'audit_logs', 'DELETE') AS can_delete, "
                    "has_table_privilege(current_user, 'audit_logs', 'TRUNCATE') AS can_truncate"
                )
            )
            .mappings()
            .one()
        )

    assert privileges["role"] == "mm_app"
    assert privileges["can_select"] is True
    assert privileges["can_insert"] is True
    assert privileges["can_update"] is False
    assert privileges["can_delete"] is False
    assert privileges["can_truncate"] is False
