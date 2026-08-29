"""End-to-end API journey (spec 24.1 "End-to-end" row).

Exercises the two user stories from the brief through the real HTTP surface:

    "I need to send BDT 2,500 to another user."
    "My friend owes me BDT 1,200. I want to collect it through the application."
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app

pytestmark = pytest.mark.integration

OPENING_BALANCE = 10_000_000  # BDT 100,000.00 in poisha
ENGINEERING_KEY = "demo-engineering-key"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def _key() -> str:
    return str(uuid.uuid4())


def _register(client: TestClient, phone: str, name: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"phone": phone, "displayName": name, "password": "correct-horse-99", "pin": "1234"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRegistrationAndFunding:
    def test_new_user_is_funded_through_the_ledger(self, client: TestClient) -> None:
        alice = _register(client, "01711000001", "Alice Rahman")

        summary = client.get("/api/v1/accounts/me", headers=_auth(alice["accessToken"]))
        assert summary.status_code == 200
        data = summary.json()["data"]
        assert data["balanceMinor"] == OPENING_BALANCE
        assert data["currency"] == "BDT"

        # The opening balance must appear as a real ledger entry, not as a
        # number written straight into the balance column.
        history = client.get("/api/v1/transactions", headers=_auth(alice["accessToken"]))
        entries = history.json()["data"]["entries"]
        assert len(entries) == 1
        assert entries[0]["kind"] == "SIGNUP_GRANT"
        assert entries[0]["direction"] == "CREDIT"
        assert entries[0]["amountMinor"] == OPENING_BALANCE
        assert entries[0]["balanceAfterMinor"] == OPENING_BALANCE

    def test_duplicate_phone_is_rejected(self, client: TestClient) -> None:
        _register(client, "01711000002", "First")
        response = client.post(
            "/api/v1/auth/register",
            json={
                "phone": "01711000002",
                "displayName": "Second",
                "password": "correct-horse-99",
                "pin": "1234",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"


class TestSendMoney:
    def test_send_2500_taka(self, client: TestClient) -> None:
        alice = _register(client, "01711000010", "Alice")
        _register(client, "01711000011", "Bob Hasan")

        # The confirmation step: the sender sees who they are about to pay.
        lookup = client.get(
            "/api/v1/accounts/lookup",
            params={"phone": "01711000011"},
            headers=_auth(alice["accessToken"]),
        )
        assert lookup.status_code == 200
        assert lookup.json()["data"]["displayName"] == "Bob Hasan"

        response = client.post(
            "/api/v1/transfers",
            json={
                "recipientPhone": "01711000011",
                "amountMinor": 250_000,  # BDT 2,500.00
                "pin": "1234",
                "note": "Lunch",
            },
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True
        assert body["meta"]["idempotentReplay"] is False
        assert body["data"]["reference"].startswith("TRX-")
        assert body["data"]["status"] == "PENDING_UNDO"
        assert body["data"]["undoExpiresAt"]
        assert body["data"]["senderBalanceMinor"] == OPENING_BALANCE - 250_000

    def test_double_tap_with_same_key_sends_once(self, client: TestClient) -> None:
        alice = _register(client, "01711000020", "Alice")
        _register(client, "01711000021", "Bob")

        key = _key()
        payload = {"recipientPhone": "01711000021", "amountMinor": 250_000, "pin": "1234"}
        headers = {**_auth(alice["accessToken"]), "Idempotency-Key": key}

        first = client.post("/api/v1/transfers", json=payload, headers=headers)
        assert first.status_code == 201
        assert first.json()["meta"]["idempotentReplay"] is False

        for _ in range(5):
            repeat = client.post("/api/v1/transfers", json=payload, headers=headers)
            # A replay is not a creation, so it answers 200, not 201.
            assert repeat.status_code == 200
            assert repeat.json()["meta"]["idempotentReplay"] is True
            assert repeat.json()["data"]["reference"] == first.json()["data"]["reference"]

        balance = client.get("/api/v1/accounts/me", headers=_auth(alice["accessToken"]))
        assert balance.json()["data"]["balanceMinor"] == OPENING_BALANCE - 250_000

    def test_missing_idempotency_key_is_rejected(self, client: TestClient) -> None:
        alice = _register(client, "01711000030", "Alice")
        _register(client, "01711000031", "Bob")
        response = client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01711000031", "amountMinor": 1000, "pin": "1234"},
            headers=_auth(alice["accessToken"]),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_wrong_pin_does_not_move_money(self, client: TestClient) -> None:
        alice = _register(client, "01711000040", "Alice")
        _register(client, "01711000041", "Bob")
        response = client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01711000041", "amountMinor": 1000, "pin": "9999"},
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )
        assert response.status_code == 400
        balance = client.get("/api/v1/accounts/me", headers=_auth(alice["accessToken"]))
        assert balance.json()["data"]["balanceMinor"] == OPENING_BALANCE

    def test_insufficient_funds_is_422(self, client: TestClient) -> None:
        alice = _register(client, "01711000050", "Alice")
        _register(client, "01711000051", "Bob")
        response = client.post(
            "/api/v1/transfers",
            json={
                "recipientPhone": "01711000051",
                "amountMinor": OPENING_BALANCE + 1,
                "pin": "1234",
            },
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
        assert response.json()["error"]["retryable"] is False


class TestMoneyRequest:
    def test_collect_1200_taka_from_a_friend(self, client: TestClient) -> None:
        alice = _register(client, "01711000060", "Alice")
        bob = _register(client, "01711000061", "Bob")

        created = client.post(
            "/api/v1/money-requests",
            json={"payerPhone": "01711000061", "amountMinor": 120_000, "note": "Dinner"},
            headers=_auth(alice["accessToken"]),
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["data"]["requestId"]

        # It lands in Bob's inbox.
        inbox = client.get(
            "/api/v1/money-requests",
            params={"direction": "incoming", "status": "PENDING"},
            headers=_auth(bob["accessToken"]),
        )
        assert [r["requestId"] for r in inbox.json()["data"]["requests"]] == [request_id]

        accepted = client.post(
            f"/api/v1/money-requests/{request_id}/accept",
            json={"pin": "1234"},
            headers={**_auth(bob["accessToken"]), "Idempotency-Key": _key()},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["status"] == "ACCEPTED"
        assert accepted.json()["data"]["transferReference"].startswith("TRX-")

        alice_balance = client.get(
            "/api/v1/accounts/me", headers=_auth(alice["accessToken"])
        ).json()["data"]["balanceMinor"]
        bob_balance = client.get(
            "/api/v1/accounts/me", headers=_auth(bob["accessToken"])
        ).json()["data"]["balanceMinor"]
        assert alice_balance == OPENING_BALANCE + 120_000
        assert bob_balance == OPENING_BALANCE - 120_000

    def test_accepting_twice_is_refused(self, client: TestClient) -> None:
        alice = _register(client, "01711000070", "Alice")
        bob = _register(client, "01711000071", "Bob")
        request_id = client.post(
            "/api/v1/money-requests",
            json={"payerPhone": "01711000071", "amountMinor": 50_000},
            headers=_auth(alice["accessToken"]),
        ).json()["data"]["requestId"]

        first = client.post(
            f"/api/v1/money-requests/{request_id}/accept",
            json={"pin": "1234"},
            headers={**_auth(bob["accessToken"]), "Idempotency-Key": _key()},
        )
        assert first.status_code == 200

        # A *different* idempotency key, so this is a genuinely new attempt to
        # settle an already-settled request - the status latch must refuse it.
        second = client.post(
            f"/api/v1/money-requests/{request_id}/accept",
            json={"pin": "1234"},
            headers={**_auth(bob["accessToken"]), "Idempotency-Key": _key()},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "REQUEST_ALREADY_HANDLED"

        bob_balance = client.get(
            "/api/v1/accounts/me", headers=_auth(bob["accessToken"])
        ).json()["data"]["balanceMinor"]
        assert bob_balance == OPENING_BALANCE - 50_000

    def test_cannot_request_from_yourself(self, client: TestClient) -> None:
        alice = _register(client, "01711000080", "Alice")
        response = client.post(
            "/api/v1/money-requests",
            json={"payerPhone": "01711000080", "amountMinor": 1000},
            headers=_auth(alice["accessToken"]),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SELF_TRANSFER_NOT_ALLOWED"


class TestAuthorizationAndValidation:
    def test_unauthenticated_access_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/accounts/me").status_code == 401

    def test_unknown_field_is_rejected(self, client: TestClient) -> None:
        """Spec 21.3: allowlist validation. Unknown keys are an error."""
        response = client.post(
            "/api/v1/auth/register",
            json={
                "phone": "01711000090",
                "displayName": "Mallory",
                "password": "correct-horse-99",
                "pin": "1234",
                "balanceMinor": 999_999_999,  # not a field we accept
            },
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("index", "amount"),
        list(enumerate([0, -1, "2500", 2.5, 10**13])),
        ids=["zero", "negative", "string", "float", "absurd"],
    )
    def test_bad_amounts_are_rejected(
        self, client: TestClient, index: int, amount: object
    ) -> None:
        # A deterministic phone per case: hash() is salted per process, so
        # deriving one from the amount would produce a different number on
        # every run and occasionally collide.
        alice = _register(client, f"017119900{index:02d}", "Alice")
        response = client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01799999999", "amountMinor": amount, "pin": "1234"},
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )
        assert response.status_code == 400, f"{amount!r} should be rejected at the edge"

    def test_cannot_read_another_users_receipt(self, client: TestClient) -> None:
        alice = _register(client, "01711000100", "Alice")
        _register(client, "01711000101", "Bob")
        mallory = _register(client, "01711000102", "Mallory")

        reference = client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01711000101", "amountMinor": 5_000, "pin": "1234"},
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        ).json()["data"]["reference"]

        # Spec 21.2: object-level authorization. 404, not 403 - confirming the
        # reference exists would itself leak information.
        response = client.get(
            f"/api/v1/transfers/{reference}", headers=_auth(mallory["accessToken"])
        )
        assert response.status_code == 404


class TestReconciliationEndpoint:
    def test_books_balance_after_a_full_journey(self, client: TestClient) -> None:
        alice = _register(client, "01711000110", "Alice")
        bob = _register(client, "01711000111", "Bob")

        client.post(
            "/api/v1/transfers",
            json={"recipientPhone": "01711000111", "amountMinor": 250_000, "pin": "1234"},
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )
        request_id = client.post(
            "/api/v1/money-requests",
            json={"payerPhone": "01711000110", "amountMinor": 120_000},
            headers=_auth(bob["accessToken"]),
        ).json()["data"]["requestId"]
        client.post(
            f"/api/v1/money-requests/{request_id}/accept",
            json={"pin": "1234"},
            headers={**_auth(alice["accessToken"]), "Idempotency-Key": _key()},
        )

        report = client.get(
            "/api/v1/engineering/reconcile", headers={"X-Engineering-Key": ENGINEERING_KEY}
        )
        assert report.status_code == 200
        data = report.json()["data"]

        assert data["balanced"] is True
        assert data["unbalanced_ledger_transactions"] == 0
        assert data["balance_mismatches"] == 0
        assert data["negative_user_accounts"] == 0
        assert data["system_wide_ledger_sum_minor"] == 0
        # Money in circulation is exactly what was issued to the two users.
        assert data["money_in_circulation_minor"] == 2 * OPENING_BALANCE
        assert data["total_user_balance_minor"] + data["total_held_minor"] == (
            2 * OPENING_BALANCE
        )

    def test_reconcile_requires_the_engineering_key(self, client: TestClient) -> None:
        assert client.get("/api/v1/engineering/reconcile").status_code == 403
