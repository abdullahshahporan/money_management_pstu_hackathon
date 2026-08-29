"""End-to-end proofs for Undo, SafePay and community Spot-Me."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import create_app
from apps.scheduler_worker.main import run_due_tasks

pytestmark = pytest.mark.integration

OPENING = 10_000_000


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


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


def _headers(user: dict, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user['accessToken']}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _balance(client: TestClient, user: dict) -> int:
    return client.get("/api/v1/accounts/me", headers=_headers(user)).json()["data"][
        "balanceMinor"
    ]


def _send(
    client: TestClient,
    sender: dict,
    recipient_phone: str,
    amount: int,
    *,
    key: str | None = None,
) -> dict:
    response = client.post(
        "/api/v1/transfers",
        json={"recipientPhone": recipient_phone, "amountMinor": amount, "pin": "1234"},
        headers=_headers(sender, key or _key()),
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


class TestUndoSettlement:
    def test_hold_is_spend_proof_and_undo_is_idempotent(self, client: TestClient) -> None:
        alice = _register(client, "01712000001", "Alice")
        bob = _register(client, "01712000002", "Bob")
        send_key = _key()

        first = _send(client, alice, "01712000002", 250_000, key=send_key)
        data = first["data"]
        assert data["status"] == "PENDING_UNDO"
        assert _balance(client, alice) == OPENING - 250_000
        assert _balance(client, bob) == OPENING

        replay = _send(client, alice, "01712000002", 250_000, key=send_key)
        assert replay["meta"]["idempotentReplay"] is True
        assert replay["data"]["transferId"] == data["transferId"]

        undo_key = _key()
        undone = client.post(
            f"/api/v1/transfers/{data['transferId']}/undo",
            headers=_headers(alice, undo_key),
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["data"]["status"] == "REFUNDED"
        again = client.post(
            f"/api/v1/transfers/{data['transferId']}/undo",
            headers=_headers(alice, undo_key),
        )
        assert again.status_code == 200
        assert again.json()["meta"]["idempotentReplay"] is True
        assert _balance(client, alice) == OPENING
        assert _balance(client, bob) == OPENING

    def test_server_worker_settles_after_client_is_gone(self, client: TestClient) -> None:
        alice = _register(client, "01712000003", "Alice")
        bob = _register(client, "01712000004", "Bob")
        data = _send(client, alice, "01712000004", 80_000)["data"]
        container = client.app.state.container

        with container.session_factory() as session:
            session.execute(
                text(
                    "UPDATE scheduled_tasks SET due_at = now() - interval '1 second' "
                    "WHERE resource_id = :id"
                ),
                {"id": data["transferId"]},
            )
            session.commit()
        with container.session_factory() as session:
            assert run_due_tasks(session, container) == 1
            session.commit()
        with container.session_factory() as session:
            assert run_due_tasks(session, container) == 0
            session.commit()

        assert _balance(client, alice) == OPENING - 80_000
        assert _balance(client, bob) == OPENING + 80_000
        receipt = client.get(
            f"/api/v1/transfers/{data['reference']}", headers=_headers(alice)
        )
        assert receipt.json()["data"]["status"] == "SUCCEEDED"


class TestSafePay:
    def test_delivery_code_releases_real_escrow(self, client: TestClient) -> None:
        buyer = _register(client, "01712000010", "Buyer")
        seller = _register(client, "01712000011", "Seller")
        created = client.post(
            "/api/v1/safepay",
            json={
                "sellerPhone": "01712000011",
                "amountMinor": 300_000,
                "pin": "1234",
                "description": "Used monitor",
            },
            headers=_headers(buyer, _key()),
        )
        assert created.status_code == 201, created.text
        escrow = created.json()["data"]
        assert len(escrow["deliveryCode"]) == 6
        assert _balance(client, buyer) == OPENING - 300_000
        assert _balance(client, seller) == OPENING

        shipped = client.post(
            f"/api/v1/safepay/{escrow['escrowId']}/ship",
            json={"courier": "pathao", "trackingNumber": "PTH-10001"},
            headers=_headers(seller, _key()),
        )
        assert shipped.status_code == 200, shipped.text
        released = client.post(
            f"/api/v1/safepay/{escrow['escrowId']}/release-code",
            json={"deliveryCode": escrow["deliveryCode"]},
            headers=_headers(seller, _key()),
        )
        assert released.status_code == 200, released.text
        assert released.json()["data"]["status"] == "RELEASED"
        assert _balance(client, seller) == OPENING + 300_000

    def test_dispute_freezes_then_admin_refunds(self, client: TestClient) -> None:
        buyer = _register(client, "01712000012", "Buyer")
        _register(client, "01712000013", "Seller")
        escrow = client.post(
            "/api/v1/safepay",
            json={"sellerPhone": "01712000013", "amountMinor": 90_000, "pin": "1234"},
            headers=_headers(buyer, _key()),
        ).json()["data"]
        disputed = client.post(
            f"/api/v1/safepay/{escrow['escrowId']}/dispute",
            json={"reason": "Parcel never arrived at my address"},
            headers=_headers(buyer, _key()),
        )
        assert disputed.json()["data"]["status"] == "DISPUTED"
        queue = client.get(
            "/api/v1/engineering/safepay/disputes",
            headers={"X-Engineering-Key": "demo-engineering-key"},
        )
        assert queue.status_code == 200, queue.text
        ticket = queue.json()["data"]["disputes"][0]
        assert ticket["escrowId"] == escrow["escrowId"]
        assert ticket["buyerName"] == "Buyer"
        assert ticket["sellerName"] == "Seller"
        resolved = client.post(
            f"/api/v1/engineering/safepay/{escrow['escrowId']}/resolve",
            json={
                "decision": "REFUND",
                "note": "Courier confirms parcel was never handed over",
                "banBuyer": False,
            },
            headers={"X-Engineering-Key": "demo-engineering-key", "Idempotency-Key": _key()},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["data"]["status"] == "REFUNDED"
        assert _balance(client, buyer) == OPENING
        empty_queue = client.get(
            "/api/v1/engineering/safepay/disputes",
            headers={"X-Engineering-Key": "demo-engineering-key"},
        )
        assert empty_queue.json()["data"]["disputes"] == []

    def test_wrong_delivery_codes_are_durably_locked(self, client: TestClient) -> None:
        buyer = _register(client, "01712000014", "Buyer")
        seller = _register(client, "01712000015", "Seller")
        escrow = client.post(
            "/api/v1/safepay",
            json={"sellerPhone": "01712000015", "amountMinor": 10_000, "pin": "1234"},
            headers=_headers(buyer, _key()),
        ).json()["data"]
        wrong_code = "000000" if escrow["deliveryCode"] != "000000" else "000001"
        statuses = []
        for _ in range(5):
            response = client.post(
                f"/api/v1/safepay/{escrow['escrowId']}/release-code",
                json={"deliveryCode": wrong_code},
                headers=_headers(seller, _key()),
            )
            statuses.append(response.status_code)
        assert statuses == [400, 400, 400, 400, 423]
        detail = client.get(
            f"/api/v1/safepay/{escrow['escrowId']}", headers=_headers(seller)
        )
        assert detail.json()["data"]["status"] == "AWAITING_SHIPMENT"

    def test_signed_courier_event_can_release_immediately(self, client: TestClient) -> None:
        buyer = _register(client, "01712000016", "Buyer")
        seller = _register(client, "01712000017", "Seller")
        escrow = client.post(
            "/api/v1/safepay",
            json={"sellerPhone": "01712000017", "amountMinor": 40_000, "pin": "1234"},
            headers=_headers(buyer, _key()),
        ).json()["data"]
        client.post(
            f"/api/v1/safepay/{escrow['escrowId']}/ship",
            json={"courier": "redx", "trackingNumber": "RDX-9000"},
            headers=_headers(seller, _key()),
        )
        event_id = _key()
        signed = f"redx|{event_id}|RDX-9000|DELIVERED|true".encode()
        secret = f"{client.app.state.container.settings.jwt_secret}:courier".encode()
        signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        event = client.post(
            "/api/v1/courier/webhooks/redx",
            json={
                "eventId": event_id,
                "trackingNumber": "RDX-9000",
                "status": "DELIVERED",
                "releaseImmediately": True,
            },
            headers={"X-Courier-Signature": signature},
        )
        assert event.status_code == 200, event.text
        assert event.json()["data"]["status"] == "RELEASED"
        assert _balance(client, seller) == OPENING + 40_000

    def test_courier_delivery_can_start_72_hour_auto_release(self, client: TestClient) -> None:
        buyer = _register(client, "01712000018", "Buyer")
        seller = _register(client, "01712000019", "Seller")
        escrow = client.post(
            "/api/v1/safepay",
            json={"sellerPhone": "01712000019", "amountMinor": 25_000, "pin": "1234"},
            headers=_headers(buyer, _key()),
        ).json()["data"]
        client.post(
            f"/api/v1/safepay/{escrow['escrowId']}/ship",
            json={"courier": "pathao", "trackingNumber": "PTH-DELAY-1"},
            headers=_headers(seller, _key()),
        )
        event_id = _key()
        signed = f"pathao|{event_id}|PTH-DELAY-1|DELIVERED|false".encode()
        secret = f"{client.app.state.container.settings.jwt_secret}:courier".encode()
        signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        delivered = client.post(
            "/api/v1/courier/webhooks/pathao",
            json={
                "eventId": event_id,
                "trackingNumber": "PTH-DELAY-1",
                "status": "DELIVERED",
                "releaseImmediately": False,
            },
            headers={"X-Courier-Signature": signature},
        )
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["data"]["status"] == "DELIVERED"
        assert delivered.json()["data"]["autoReleaseAt"]
        assert _balance(client, seller) == OPENING

        container = client.app.state.container
        with container.session_factory() as session:
            session.execute(
                text(
                    "UPDATE scheduled_tasks SET due_at = now() - interval '1 second' "
                    "WHERE resource_id = :id"
                ),
                {"id": escrow["escrowId"]},
            )
            session.commit()
        with container.session_factory() as session:
            assert run_due_tasks(session, container) == 1
            session.commit()
        assert _balance(client, seller) == OPENING + 25_000


class TestCommunitySpotMe:
    def test_exact_shortfall_draw_and_lien_on_undo_refund(self, client: TestClient) -> None:
        sponsor = _register(client, "01712000020", "Sponsor")
        borrower = _register(client, "01712000021", "Borrower")
        _register(client, "01712000022", "Recipient")
        _send(client, borrower, "01712000022", 9_990_000)
        assert _balance(client, borrower) == 10_000

        pool = client.post(
            "/api/v1/overdraft/pools",
            json={"amountMinor": 50_000, "pin": "1234"},
            headers=_headers(sponsor, _key()),
        )
        assert pool.status_code == 201, pool.text
        grant = client.post(
            "/api/v1/overdraft/grants",
            json={
                "beneficiaryPhone": "01712000021",
                "maxDrawMinor": 50_000,
                "pin": "1234",
            },
            headers=_headers(sponsor, _key()),
        )
        assert grant.status_code == 201, grant.text

        sent = _send(client, borrower, "01712000022", 30_000)["data"]
        assert sent["overdraftUsed"] is True
        assert sent["overdraft"]["amountMinor"] == 20_000
        assert _balance(client, borrower) == 0
        summary = client.get("/api/v1/overdraft", headers=_headers(borrower)).json()[
            "data"
        ]
        assert summary["debts"][0]["outstandingMinor"] == 20_000

        undone = client.post(
            f"/api/v1/transfers/{sent['transferId']}/undo",
            headers=_headers(borrower, _key()),
        )
        assert undone.status_code == 200, undone.text
        # The refund is an incoming credit. The 50% lien repays 15,000 and
        # leaves 15,000 available to the borrower.
        assert _balance(client, borrower) == 15_000
        summary = client.get("/api/v1/overdraft", headers=_headers(borrower)).json()[
            "data"
        ]
        assert summary["debts"][0]["outstandingMinor"] == 5_000
        sponsor_summary = client.get(
            "/api/v1/overdraft", headers=_headers(sponsor)
        ).json()["data"]
        assert sponsor_summary["sponsoredPool"]["balanceMinor"] == 45_000


class TestPinLockout:
    def test_failed_pin_attempts_survive_rollback_boundary(self, client: TestClient) -> None:
        alice = _register(client, "01712000030", "Alice")
        _register(client, "01712000031", "Bob")
        statuses = []
        for _ in range(5):
            response = client.post(
                "/api/v1/transfers",
                json={
                    "recipientPhone": "01712000031",
                    "amountMinor": 100,
                    "pin": "9999",
                },
                headers=_headers(alice, _key()),
            )
            statuses.append(response.status_code)
        assert statuses == [400, 400, 400, 400, 423]
        sixth = client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01712000031", "amountMinor": 100, "pin": "1234"},
            headers=_headers(alice, _key()),
        )
        assert sixth.status_code == 423
        assert _balance(client, alice) == OPENING
