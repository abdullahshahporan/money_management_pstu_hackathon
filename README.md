# Taka — Money Movement Platform

A closed-ecosystem digital wallet where people send money to each other and collect money
they are owed. Built for the PSTU IT Carnival 2026 hackathon.

Every taka in this system can be traced to a balanced double-entry ledger posting. The
central claim is not that transfers work — it is that they stay correct when things go
wrong: double taps, dropped connections, simultaneous payments, hostile input, and a
message broker that falls over mid-demo.

---

## Run it

Requires Docker. One command:

```bash
docker compose up -d --build
```

Then open **http://localhost:8080**.

| What | Where |
|---|---|
| Web app | http://localhost:8080 |
| API docs (OpenAPI) | http://localhost:8080/api/v1/docs |
| Database browser | http://localhost:8081 (server `postgres`, user `mm_owner`, password `mm_owner_dev_pw`) |
| RabbitMQ console | http://localhost:15672 (`mm` / `mm_dev_pw`) |

Load deterministic demo data:

```bash
docker compose run --rm --entrypoint python api -m scripts.seed
```

That creates four users, each funded **BDT 100,000.00**, plus a couple of transfers and one
pending money request.

| Phone | Name | Password | PIN |
|---|---|---|---|
| `01711111111` | Alice Rahman | `demo-password-2026` | `1234` |
| `01722222222` | Bob Hasan | `demo-password-2026` | `1234` |
| `01733333333` | Chowdhury Karim | `demo-password-2026` | `1234` |
| `01744444444` | Dina Akter | `demo-password-2026` | `1234` |

> Run `docker compose` from the repository root. Compose resolves `${...}` from whichever
> `.env` it finds for the invoking directory, so running it from a subdirectory can pick up
> the wrong one.

## Run the tests

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux/macOS: .venv/bin/pip

.venv/Scripts/pytest tests/ -q          # everything
.venv/Scripts/pytest tests/unit -q      # fast, no database
.venv/Scripts/pytest tests/concurrency -v   # the proofs
.venv/Scripts/ruff check .              # lint
.venv/Scripts/lint-imports              # architecture contracts
```

The integration and concurrency suites need PostgreSQL, which `docker compose up -d postgres`
provides. They run against the real engine on purpose: row locks, `CHECK` constraints,
`ON CONFLICT` and deadlock detection do not exist in a mock.

They use a **separate database**, `moneymovement_test`, created by the Postgres init script.
The suite truncates tables between tests, so pointing it at the database a running stack is
using would destroy live data — which happened once during development and wiped the seeded
demo accounts. Override with `TEST_DATABASE_URL` if you need to.

If your Postgres volume predates that init script, create it once:

```bash
docker compose exec postgres psql -U mm_owner -d postgres -c "CREATE DATABASE moneymovement_test OWNER mm_owner;"
cd backend && MIGRATION_DATABASE_URL=postgresql+psycopg://mm_owner:mm_owner_dev_pw@localhost:5432/moneymovement_test .venv/Scripts/alembic upgrade head
```

---

## What it does

- Register and receive an opening balance of BDT 100,000, posted as a real ledger transaction.
- See an authoritative balance, never a cached one.
- Look a recipient up by phone and **confirm their name before paying**.
- Send money, authorised by a transaction PIN that is separate from the login password.
- Undo a send for 10 seconds while the money is safely parked in a holding account.
- Use Conditional SafePay: buyer-funded escrow, delivery code, signed courier events,
  auto-release, disputes and admin release/refund.
- Create a pre-funded community Spot-Me pool; an approved borrower can draw only the exact
  shortfall and automatically repay from later incoming funds.
- Request money from someone; they approve, decline, or let it expire.
- Read a cursor-paginated statement with a running balance on every line.
- Retry any payment safely — the same intent can be submitted any number of times.
- Run a live integrity report that recomputes every balance from the ledger.

## Architecture

```
Browser (React)
      │
   nginx ──── serves the SPA, proxies /api, load balances
      │
      ├── api replica 1 ─┐
      └── api replica 2 ─┤
                         ├── PostgreSQL   the only source of financial truth
                         └── Redis        rate limits and caches, never money
                         
  outbox-relay ── polls outbox_events ──▶ RabbitMQ ──▶ notification-worker
  scheduler-worker ── polls scheduled_tasks ──▶ undo settlement / escrow auto-release
```

The **financial core** — accounts, balances, transfers, the ledger, idempotency and the
outbox event — is one bounded context sharing one ACID transaction. It is deliberately not
split into separate services: a timeout between "debit" and "credit" would turn a safe local
commit into a distributed transaction needing sagas and compensation, and compensation is
not equivalent to atomicity.

Full design rationale, invariants, failure matrix and scaling path: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.
The advanced feature flows and judge-ready code map: **[docs/ADVANCED_FEATURES.md](docs/ADVANCED_FEATURES.md)**.
The easy full-project presentation, demo and viva guide: **[docs/STUDENT_JUDGE_GUIDE.md](docs/STUDENT_JUDGE_GUIDE.md)**.

### Layout

```
backend/
├── apps/            api · outbox_relay · notification_worker · scheduler_worker
├── modules/
│   ├── financial_core/   ★ the money. domain / application / adapters
│   ├── identity/         registration, login, sessions, PIN
│   ├── money_request/    the request-and-approve workflow
│   ├── safepay/          escrow state machine and delivery verification
│   ├── overdraft/        community pools, grants, loans and repayment liens
│   ├── reconciliation/   independent integrity checks
│   └── audit/
├── platform_/       kernel (Money, errors, ids, clock) · database · security · messaging · web
├── alembic/         versioned migrations
└── tests/           unit · integration · concurrency
frontend/            React + Vite
```

`platform_` carries a trailing underscore so it cannot shadow Python's stdlib `platform`
module, which uvicorn and testcontainers both import.

The dependency rule — domain imports nothing outward — is enforced by `import-linter`
contracts in `pyproject.toml`, so it fails the build rather than eroding quietly. It has
already caught two real violations during development.

---

## API

Base path `/api/v1`. Every response shares one envelope:

```jsonc
{ "success": true,  "data": { }, "meta": { "requestId": "…" } }
{ "success": false, "error": { "code": "INSUFFICIENT_FUNDS", "message": "…", "retryable": false } }
```

Clients branch on `error.code`, never on `message`.

| Method | Endpoint | Notes |
|---|---|---|
| POST | `/auth/register` | creates the user, opens and funds the account, in one transaction |
| POST | `/auth/login` · `/auth/refresh` · `/auth/logout` | refresh tokens rotate; reuse revokes the family |
| GET | `/accounts/me` | authoritative balance, read from the primary |
| GET | `/accounts/lookup?phone=` | recipient confirmation; exact match only |
| POST | `/transfers` | **requires `Idempotency-Key`**; 10-second Undo hold; automatic Spot-Me fallback |
| GET/POST | `/transfers/pending-undo` · `/transfers/{id}/undo` | list or cancel an undoable send |
| GET | `/transfers/{reference}` | receipt, visible only to the two parties |
| GET | `/transactions?cursor=&limit=` | keyset pagination, never `OFFSET` |
| POST | `/money-requests` | ask someone to pay |
| GET | `/money-requests?direction=incoming\|outgoing` | inbox / outbox |
| POST | `/money-requests/{id}/accept` | **requires `Idempotency-Key`**; settles atomically |
| POST | `/money-requests/{id}/reject` · `/cancel` | |
| POST/GET | `/safepay` | create escrow / list orders |
| POST | `/safepay/{id}/ship` · `/release-code` · `/confirm-received` · `/dispute` | SafePay state transitions |
| POST | `/courier/webhooks/{courier}` | HMAC-signed delivery event; immediate or delayed release |
| GET/POST | `/overdraft` · `/overdraft/pools` · `/overdraft/pools/fund` · `/overdraft/grants` | Spot-Me pool, trusted grants and debt view |
| GET | `/health/live` · `/health/ready` | liveness ignores dependencies; readiness does not |
| GET | `/engineering/reconcile` · `/engineering/outbox` · `/engineering/scheduler` | protected by `X-Engineering-Key` |

Status codes: `400` malformed · `401` unauthenticated · `403` unauthorised · `404` not
visible · `409` state or idempotency conflict · `422` business rule rejected · `423` PIN
locked · `429` rate limited · `503` dependency unavailable.

### Sending money

```bash
curl -X POST http://localhost:8080/api/v1/transfers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"recipientPhone":"01722222222","amountMinor":250000,"pin":"1234","note":"Lunch"}'
```

`amountMinor` is **integer poisha** — BDT 2,500.00 is `250000`. No decimal ever crosses the
wire, and no float exists anywhere in the money path.

---

## What we deliberately did not build

Real bank or MFS integration · card processing · KYC/AML · live Pathao/RedX credentials and
vendor-specific polling (a signed generic courier webhook is implemented) · multi-region
active-active ledger writes · physical sharding · blockchain · a fleet of microservices ·
SMS/push delivery (the notification consumer records deliveries rather than calling a provider).

Each is either outside the brief's closed-ecosystem scope or, in the case of sharding,
something that should follow measured bottlenecks rather than precede them.
