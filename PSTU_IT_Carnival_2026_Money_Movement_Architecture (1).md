# PSTU IT CARNIVAL 2026 --- Money Movement Application

## Recommended Engineering & System Design

**Event:** PSTU IT Carnival 2026 Hackathon\
**Challenge:** Money Movement Application\
**Date:** 29 August 2026\
**Target scale:** 10+ million users within 3 years\
**Initial balance:** BDT 100,000 per registered user\
**System type:** Closed digital money ecosystem with simulated/fake
funds

------------------------------------------------------------------------

# 1. Core Objective

Build a small but trustworthy money-movement platform where users can:

-   Register and receive a simulated balance.
-   View their balance and transaction history.
-   Send money to another user.
-   Request money from another user.
-   Accept or reject money requests.
-   Cancel pending requests.
-   Track transaction status.
-   Receive transaction notifications.
-   Safely retry requests without accidentally transferring money twice.

The main engineering goal is:

> **Make money movement correct first, then make it scalable.**

Do not treat the project as a simple CRUD application.

------------------------------------------------------------------------

# 2. What the Judges Should See

The application should demonstrate two things:

## User perspective

The application should be:

-   Simple
-   Fast
-   Understandable
-   Useful
-   Reliable
-   Transparent about transaction status

## Engineering perspective

The system should demonstrate:

-   Concurrency safety
-   ACID transactions
-   Idempotency
-   Database constraints
-   SOLID principles
-   Appropriate design patterns
-   Stateless backend
-   Load balancing strategy
-   Caching strategy
-   Database replication strategy
-   Future sharding strategy
-   Asynchronous processing
-   Failure handling
-   Auditability
-   Rate limiting
-   Testing

------------------------------------------------------------------------

# 3. Recommended Architecture

For a hackathon, start with a **modular monolith**, not dozens of
microservices.

``` text
                    ┌──────────────────────┐
                    │   Web / Mobile App   │
                    └──────────┬───────────┘
                               │ HTTPS
                               ▼
                    ┌──────────────────────┐
                    │    Load Balancer     │
                    └──────────┬───────────┘
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
           ┌──────────┐  ┌──────────┐  ┌──────────┐
           │ API #1   │  │ API #2   │  │ API #3   │
           │ Stateless│  │ Stateless│  │ Stateless│
           └────┬─────┘  └────┬─────┘  └────┬─────┘
                └──────────────┼──────────────┘
                               ▼
                    ┌──────────────────────┐
                    │  Banking Application │
                    │      Modules         │
                    ├──────────────────────┤
                    │ Authentication       │
                    │ User / Account       │
                    │ Transfer             │
                    │ Money Request        │
                    │ Transaction / Ledger  │
                    │ Notification         │
                    │ Audit                │
                    └──────────┬───────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
          ┌──────────────────┐   ┌─────────────────┐
          │   PostgreSQL     │   │ Message Broker  │
          │  ACID Database   │   │ Queue / Events  │
          └────────┬─────────┘   └────────┬────────┘
                   │                      │
             ┌─────┴─────┐          ┌────┼────┐
             ▼           ▼          ▼    ▼    ▼
         Replica #1  Replica #2   SMS  Audit Analytics
```

------------------------------------------------------------------------

# 4. Why Modular Monolith First?

A hackathon has limited development time.

Starting with microservices creates unnecessary complexity:

``` text
Microservices
+ Service discovery
+ Network failures
+ Distributed transactions
+ Deployment complexity
+ Inter-service authentication
+ More monitoring
```

Instead:

``` text
                   Modular Monolith
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Account        Transfer       Request
        Module         Module         Module
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                    PostgreSQL
```

Keep modules logically independent so they can later be extracted into
microservices.

------------------------------------------------------------------------

# 5. Core Functional Modules

## 5.1 Authentication Module

Responsibilities:

-   Registration
-   Login
-   Logout
-   Password hashing
-   Token/session management
-   Basic account security

Suggested entities:

``` text
User
----
id
name
email / phone
password_hash
created_at
status
```

------------------------------------------------------------------------

## 5.2 Account Module

Responsibilities:

-   Create account
-   Show balance
-   Account status
-   Currency
-   Account ownership

``` text
Account
-------
id
user_id
account_number
balance
currency
status
version
created_at
```

Every new user starts with:

``` text
balance = BDT 100,000
```

------------------------------------------------------------------------

# 6. Money Transfer

The primary operation is:

``` text
User A
  │
  │ Send BDT 2,500
  ▼
User B
```

The transfer must be atomic.

Required result:

``` text
A balance: -2500
B balance: +2500
Transaction: SUCCESS
```

Or:

``` text
A balance: unchanged
B balance: unchanged
Transaction: FAILED
```

Never allow:

``` text
A debited
B not credited
```

------------------------------------------------------------------------

# 7. Correct Transfer Flow

``` text
User
 │
 ▼
POST /transfers
 │
 ▼
Authentication
 │
 ▼
Rate Limiting
 │
 ▼
Validate Request
 │
 ▼
Check Idempotency Key
 │
 ▼
Database Transaction
 │
 ├── Lock source account
 ├── Lock destination account
 ├── Check balance
 ├── Debit source
 ├── Credit destination
 ├── Create ledger entries
 ├── Create transaction record
 └── Create outbox event
 │
 ▼
COMMIT
 │
 ▼
Return SUCCESS
 │
 ▼
Async Notification
```

------------------------------------------------------------------------

# 8. Concurrency --- Critical Requirement

A banking system must handle simultaneous requests correctly.

Example:

``` text
Initial balance = BDT 1,000

Request A:
Withdraw BDT 800

Request B:
Withdraw BDT 700
```

Both requests may arrive at almost the same time.

A naive implementation may do:

``` text
Thread 1 → read 1000
Thread 2 → read 1000

Thread 1 → calculate 200
Thread 2 → calculate 300

Thread 1 → write 200
Thread 2 → write 300
```

This is unsafe.

The application must ensure that only a valid transaction can commit.

------------------------------------------------------------------------

# 9. Recommended Concurrency Control

Use the database as the source of truth.

Recommended options:

## Option A --- Row-level locking

Conceptually:

``` sql
SELECT *
FROM accounts
WHERE id = ?
FOR UPDATE;
```

Then:

``` text
Lock
  ↓
Validate
  ↓
Update
  ↓
Commit
  ↓
Unlock
```

## Option B --- Optimistic versioning

``` text
Account:
balance = 1000
version = 10
```

Update:

``` sql
UPDATE accounts
SET balance = 500,
    version = 11
WHERE id = ?
AND version = 10;
```

If another transaction already changed version 10:

``` text
affected rows = 0
```

Then retry or reject.

For the core money-transfer path, row-level locking/strong transactional
control is a simple choice for the hackathon.

------------------------------------------------------------------------

# 10. Never Depend Only on Application Locks

Avoid relying on:

``` java
synchronized
```

or an in-memory mutex for financial correctness.

Why?

With multiple backend servers:

``` text
              Load Balancer
             /      |      \
            ▼       ▼       ▼
         Server 1 Server 2 Server 3
```

Each server has its own memory.

A lock on Server 1 does not automatically lock Server 2.

Therefore:

> **Financial concurrency must be protected at the shared data layer.**

------------------------------------------------------------------------

# 11. Idempotency

This is one of the most important features to demonstrate.

Scenario:

``` text
User clicks SEND
      ↓
BDT 2,500 transferred
      ↓
Network timeout
      ↓
App receives no response
      ↓
User clicks SEND again
```

Without idempotency:

``` text
Transfer #1 = BDT 2,500
Transfer #2 = BDT 2,500

Total = BDT 5,000
```

With idempotency:

``` text
Request
idempotency_key = ABC123
```

First request:

``` text
ABC123 → process → SUCCESS
```

Second request:

``` text
ABC123 → already processed
        → return previous result
```

Database:

``` text
idempotency_records
-------------------
key
request_hash
transaction_id
response
created_at
```

Also add:

``` text
UNIQUE(idempotency_key)
```

------------------------------------------------------------------------

# 12. Database Design

Recommended database:

> **PostgreSQL**

Core tables:

``` text
users
accounts
transactions
ledger_entries
money_requests
idempotency_records
outbox_events
audit_logs
```

------------------------------------------------------------------------

# 13. Transaction Table

``` text
transactions
------------
id
reference_id
idempotency_key
from_account_id
to_account_id
amount
currency
status
created_at
completed_at
failure_reason
```

Possible states:

``` text
PENDING
SUCCESS
FAILED
CANCELLED
```

------------------------------------------------------------------------

# 14. Ledger

Do not depend only on a mutable balance field.

Maintain an auditable financial history.

Example:

``` text
Transfer BDT 2,500

Source Account
   Ledger: -2500

Destination Account
   Ledger: +2500
```

Conceptually:

``` text
                  Transfer
                     │
                     ▼
                  Ledger
              ┌──────┴──────┐
              ▼             ▼
        Debit -2500    Credit +2500
              │             │
              └──────┬──────┘
                     ▼
               Account State
```

The ledger provides a much better audit trail.

------------------------------------------------------------------------

# 15. Money Request Feature

Example:

``` text
Alice → requests BDT 1,200 from Bob
```

Flow:

``` text
Alice
 │
 │ Request BDT 1,200
 ▼
Bob
 │
 ├── Accept
 │     ↓
 │   Transfer
 │
 ├── Reject
 │
 └── Ignore / Expire
```

Table:

``` text
money_requests
--------------
id
requester_id
payer_id
amount
note
status
expires_at
created_at
```

Statuses:

``` text
PENDING
ACCEPTED
REJECTED
CANCELLED
EXPIRED
```

------------------------------------------------------------------------

# 16. SOLID Principles

Use SOLID throughout the backend.

## S --- Single Responsibility Principle

Avoid:

``` text
BankService
 ├── Transfer
 ├── Email
 ├── PDF
 ├── Authentication
 ├── Database
 └── Notification
```

Prefer:

``` text
TransferService
AccountService
NotificationService
AuthenticationService
StatementService
```

------------------------------------------------------------------------

## O --- Open/Closed Principle

For fee calculation:

``` text
FeeStrategy
 ├── StandardFee
 ├── StudentFee
 ├── PremiumFee
 └── BusinessFee
```

Adding a new policy should not require rewriting TransferService.

------------------------------------------------------------------------

## L --- Liskov Substitution Principle

Subtypes must respect the contract of their abstractions.

Do not create an account subtype that unexpectedly violates the behavior
expected from `Account`.

------------------------------------------------------------------------

## I --- Interface Segregation Principle

Avoid one giant interface:

``` text
BankService
```

Prefer:

``` text
TransferService
AccountService
NotificationService
StatementService
```

------------------------------------------------------------------------

## D --- Dependency Inversion Principle

Bad:

``` text
TransferService
      ↓
PostgresDatabase
```

Better:

``` text
TransferService
      ↓
AccountRepository
      ↑
PostgresAccountRepository
```

Use Dependency Injection.

------------------------------------------------------------------------

# 17. Design Patterns

Use patterns where they solve actual problems.

  Pattern                Recommended use
  ---------------------- -----------------------------------------------
  Strategy               Fee/commission/limit policies
  Factory                Creating payment/notification implementations
  Adapter                External service/API integration
  Facade                 Simplifying complex operations
  Observer / Pub-Sub     Transaction events
  State                  Transaction/request lifecycle
  Repository             Database abstraction
  Dependency Injection   Loose coupling
  Outbox                 Reliable event publishing
  Circuit Breaker        External-service failure
  Saga                   Distributed workflows later

Do not add patterns only for decoration.

------------------------------------------------------------------------

# 18. Strategy Pattern Example

Instead of:

``` java
if(type == STUDENT) ...
else if(type == PREMIUM) ...
else if(type == BUSINESS) ...
```

Use:

``` text
                 FeeStrategy
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   StudentFee   PremiumFee   BusinessFee
```

Then:

``` text
TransferService
      │
      ▼
FeeStrategy
```

Adding:

``` text
CorporateFeeStrategy
```

does not require changing the transfer logic.

------------------------------------------------------------------------

# 19. Repository Pattern

Keep database details away from business logic.

``` text
TransferService
      │
      ▼
AccountRepository
      │
      ▼
PostgresAccountRepository
      │
      ▼
PostgreSQL
```

This improves:

-   Testability
-   Maintainability
-   Separation of concerns
-   Future database migration

------------------------------------------------------------------------

# 20. Outbox Pattern

Avoid this unsafe sequence:

``` text
DB update
   ↓
Publish message
```

If the DB succeeds but message publishing fails, the systems become
inconsistent.

Instead:

``` text
BEGIN TRANSACTION

Update account
Create ledger entry
Create transaction
Insert outbox event

COMMIT
```

Then:

``` text
Outbox Worker
      ↓
Read event
      ↓
Message Broker
      ↓
Notification / Audit / Analytics
```

Architecture:

``` text
             PostgreSQL
            /          \
           ▼            ▼
      Financial Data   Outbox
                         │
                         ▼
                    Event Worker
                         │
                         ▼
                    Message Broker
                    /      |      \
                   ▼       ▼       ▼
                Notify   Audit  Analytics
```

------------------------------------------------------------------------

# 21. Asynchronous Processing

Do not make the transfer wait for:

``` text
SMS
Email
Push notification
Analytics
```

Correct approach:

``` text
Transfer
   │
   ▼
ACID Commit
   │
   ▼
Transaction SUCCESS
   │
   ▼
Event
   │
   ▼
Queue
 ┌─┴──────────────┐
 ▼                ▼
Notification    Analytics
```

The financial transaction remains synchronous and strongly consistent.

Secondary operations can be eventually consistent.

------------------------------------------------------------------------

# 22. CAP and Consistency

CAP:

``` text
C = Consistency
A = Availability
P = Partition Tolerance
```

For financial operations, correctness is more important than blindly
returning a result during a partition.

Example:

``` text
Network partition
       ↓
Cannot verify current balance
       ↓
Do NOT guess
       ↓
Reject / delay transaction
```

Use strong consistency for:

``` text
Balance
Available balance
Transfer
Ledger
Transaction status
Financial limits
```

Eventual consistency can be acceptable for:

``` text
Analytics
Notifications
Statistics
Recommendations
Search
```

------------------------------------------------------------------------

# 23. Load Balancing

Design the API tier to be stateless.

``` text
                 Load Balancer
                /      |      \
               ▼       ▼       ▼
             API-1   API-2   API-3
```

Any server should be able to process any request.

Do not store critical session state only in:

``` text
Server 1 RAM
```

Use:

``` text
Token-based authentication
+
Shared database
+
Distributed cache when needed
```

This allows horizontal scaling.

------------------------------------------------------------------------

# 24. Horizontal Scaling

If traffic increases:

``` text
1 API Server
      ↓
3 API Servers
      ↓
20 API Servers
      ↓
100 API Servers
```

The load balancer distributes requests.

The backend should remain stateless so adding servers does not require
changing business logic.

------------------------------------------------------------------------

# 25. Caching

Use caching for data where slight staleness is acceptable.

Good candidates:

``` text
User profile
Branch information
Static configuration
Frequently requested non-financial data
```

Be careful with:

``` text
Current balance
Available balance
Pending financial state
```

The database should remain the source of truth for financial state.

------------------------------------------------------------------------

# 26. Database Replication

As reads increase:

``` text
                  Primary
                     │
              ┌──────┴──────┐
              ▼             ▼
          Replica 1      Replica 2
```

Writes:

``` text
→ Primary
```

Read-heavy operations:

``` text
→ Replicas
```

However, replica lag can produce stale results.

For critical read-after-write operations such as immediately checking
the result of a transfer, use the appropriate consistency strategy
rather than blindly reading from a replica.

------------------------------------------------------------------------

# 27. Database Sharding

Do not shard the database on day one unless required.

Initial:

``` text
                PostgreSQL
                    │
              Primary + Replica
```

Later:

``` text
                  DB Router
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Shard 1     Shard 2     Shard 3
```

Potential partition key:

``` text
user_id
```

But choose the shard key based on transaction access patterns.

------------------------------------------------------------------------

# 28. Important Sharding Problem

Suppose:

``` text
Account A → Shard 1
Account B → Shard 7
```

A transfer becomes:

``` text
Shard 1
  ↓
Debit

Shard 7
  ↓
Credit
```

Now one business transaction crosses multiple databases.

This introduces distributed-transaction complexity.

Therefore:

> **Do not choose a shard key without considering the application's most
> important transactions.**

For the hackathon, keep financial transactions within a single
relational database.

Mention sharding as the **future scalability strategy**.

------------------------------------------------------------------------

# 29. Rate Limiting

Protect APIs against abuse.

Example:

``` text
POST /transfer
```

could have limits such as:

``` text
Per user
Per IP
Per device/session
Per time window
```

Architecture:

``` text
User
 │
 ▼
API Gateway
 │
 ▼
Rate Limiter
 │
 ├── Limit exceeded → 429
 │
 └── Allowed
       ↓
    Backend
```

Use token bucket or sliding-window approaches if implementing
distributed rate limiting.

------------------------------------------------------------------------

# 30. Reliability

Important mechanisms:

``` text
Health checks
Timeouts
Retries
Circuit breakers
Redundancy
Database backups
Failover
Dead-letter queues
Monitoring
Alerting
Audit logs
```

Do not blindly retry money transfers.

A retry should be combined with idempotency.

Correct:

``` text
Transfer request
     ↓
Idempotency key
     ↓
Retry safely
```

------------------------------------------------------------------------

# 31. Circuit Breaker

Use it for external dependencies.

Example:

``` text
Notification Service
       ↓
      FAIL
       ↓
Circuit Breaker
       ↓
Stop repeatedly calling failing service
```

The transfer itself should not fail simply because an optional
notification provider is unavailable.

------------------------------------------------------------------------

# 32. Security

Even though the challenge uses fake money, demonstrate production-style
security thinking.

Minimum:

-   HTTPS
-   Password hashing
-   Secure authentication
-   Authorization
-   Input validation
-   SQL injection protection
-   Rate limiting
-   Secure token handling
-   Audit logs
-   No sensitive data in logs
-   Server-side balance validation
-   Transaction authorization
-   Proper error handling

Never trust:

``` text
Client-provided balance
```

The server/database must calculate and verify financial state.

------------------------------------------------------------------------

# 33. API Design

Example endpoints:

## Authentication

``` text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
```

## Account

``` text
GET /api/account
GET /api/account/balance
```

## Transfers

``` text
POST /api/transfers
GET /api/transfers
GET /api/transfers/{id}
```

## Money Requests

``` text
POST /api/requests
GET /api/requests
POST /api/requests/{id}/accept
POST /api/requests/{id}/reject
POST /api/requests/{id}/cancel
```

## Transactions

``` text
GET /api/transactions
GET /api/transactions/{id}
```

------------------------------------------------------------------------

# 34. Example Transfer Request

``` json
{
  "recipientId": "USER-002",
  "amount": 2500,
  "currency": "BDT",
  "note": "Lunch"
}
```

Header:

``` text
Idempotency-Key: 8f6c2d...
```

Response:

``` json
{
  "transactionId": "TX-20260829-001",
  "status": "SUCCESS",
  "amount": 2500,
  "currency": "BDT"
}
```

------------------------------------------------------------------------

# 35. Financial Invariants

These rules should never be violated.

## Invariant 1

A user cannot transfer more than their available balance.

``` text
amount <= available_balance
```

## Invariant 2

A successful transfer must debit and credit exactly once.

``` text
Debit = -X
Credit = +X
```

## Invariant 3

A failed transfer must not change financial balances.

## Invariant 4

The same idempotency key must not create multiple transfers.

## Invariant 5

Every successful transfer must have an auditable transaction record.

## Invariant 6

Client-side balance must never be trusted.

------------------------------------------------------------------------

# 36. Failure Scenarios to Demonstrate

Judges may ask:

### Scenario 1 --- Double click

``` text
User clicks twice
```

Answer:

``` text
Idempotency key prevents duplicate transfer.
```

### Scenario 2 --- Two simultaneous withdrawals

``` text
Two requests
      ↓
Same account
```

Answer:

``` text
Database transaction + row-level locking/concurrency control.
```

### Scenario 3 --- Server crashes after DB commit

Answer:

``` text
Transaction is durable.
Outbox event can be processed after recovery.
```

### Scenario 4 --- Notification service fails

Answer:

``` text
Transfer remains successful.
Notification is asynchronous.
Retry/DLQ handles notification failure.
```

### Scenario 5 --- Traffic increases 100×

Answer:

``` text
Load Balancer
→ Stateless API instances
→ Cache
→ Read replicas
→ Queue/workers
→ Database partitioning/sharding when necessary
```

### Scenario 6 --- Database becomes unavailable

Answer:

``` text
Failover / replica strategy
+
backup/recovery
+
health monitoring
```

Do not invent a successful financial result when the authoritative
database cannot confirm it.

------------------------------------------------------------------------

# 37. Testing Strategy

## Unit Testing

Test:

``` text
Fee calculation
Validation
Transfer rules
Request state transitions
Authorization
```

## Integration Testing

Test:

``` text
API
 ↓
Service
 ↓
Database
```

## Concurrency Testing

Important cases:

``` text
Two withdrawals simultaneously
Two transfers simultaneously
Repeated identical requests
Concurrent request acceptance
```

## Load Testing

Simulate:

``` text
100 users
1,000 users
10,000 concurrent requests
```

Measure:

``` text
Latency
Throughput
Error rate
Database load
CPU
Memory
```

## Failure Testing

Test:

``` text
Database unavailable
Queue unavailable
Notification service unavailable
Network timeout
Server restart
Duplicate request
```

------------------------------------------------------------------------

# 38. Observability

Use three major categories:

``` text
Logs
Metrics
Traces
```

## Logs

Examples:

``` text
TRANSFER_CREATED
TRANSFER_SUCCESS
TRANSFER_FAILED
REQUEST_ACCEPTED
AUTH_FAILED
```

Never log sensitive secrets.

## Metrics

Track:

``` text
requests/sec
transfer success rate
transfer failure rate
API latency
database latency
queue depth
CPU
memory
```

## Tracing

For a request:

``` text
Mobile
 ↓
API
 ↓
TransferService
 ↓
Database
 ↓
Outbox
 ↓
Queue
 ↓
Notification
```

Tracing helps identify where latency or failures occur.

------------------------------------------------------------------------

# 39. Recommended Project Structure

For a Java/Spring-style backend:

``` text
backend/
│
├── auth/
│   ├── controller/
│   ├── service/
│   ├── repository/
│   └── model/
│
├── account/
│   ├── controller/
│   ├── service/
│   ├── repository/
│   └── model/
│
├── transfer/
│   ├── controller/
│   ├── service/
│   ├── repository/
│   ├── strategy/
│   └── model/
│
├── moneyrequest/
│   ├── controller/
│   ├── service/
│   ├── repository/
│   └── model/
│
├── transaction/
│   ├── service/
│   ├── repository/
│   └── model/
│
├── notification/
│
├── audit/
│
├── outbox/
│
├── common/
│   ├── exception/
│   ├── security/
│   ├── validation/
│   └── config/
│
└── application/
```

The exact framework can change. The architectural separation is what
matters.

------------------------------------------------------------------------

# 40. Recommended Development Order

Because the hackathon time is limited, implement in this order.

## Phase 1 --- Working MVP

``` text
1. Registration
2. Login
3. Initial BDT 100,000
4. Account balance
5. Send money
6. Transaction history
```

## Phase 2 --- Trustworthiness

``` text
7. Database transactions
8. Concurrency control
9. Idempotency
10. Database constraints
11. Ledger
12. Money requests
```

## Phase 3 --- Engineering Quality

``` text
13. SOLID
14. Strategy
15. Repository
16. Factory/Adapter where useful
17. Outbox
18. Async notification
```

## Phase 4 --- Scalability

``` text
19. Stateless API
20. Load balancing
21. Rate limiting
22. Cache where appropriate
23. Read replicas
24. Background workers
```

## Phase 5 --- Demonstration

``` text
25. Concurrency test
26. Duplicate-request test
27. Load test
28. Failure demonstration
29. Architecture diagram
30. Trade-off explanation
```

------------------------------------------------------------------------

# 41. What NOT to Build

Do not waste hackathon time on:

``` text
Real bank integration
Real payment gateway
Real card processing
Complex KYC
20 microservices
Multi-region deployment
Kubernetes cluster
Blockchain
Complex AI fraud detection
```

unless the core money movement system is already working.

------------------------------------------------------------------------

# 42. Best Hackathon Architecture

For the actual competition, the sweet spot is:

``` text
                 WEB / MOBILE
                      │
                      ▼
                Load Balancer
                      │
              ┌───────┴───────┐
              ▼               ▼
         Backend #1       Backend #2
          Stateless        Stateless
              │               │
              └───────┬───────┘
                      ▼
                PostgreSQL
                      │
             ┌────────┴────────┐
             ▼                 ▼
        Financial Data       Outbox
                                 │
                                 ▼
                           Message Queue
                           /     |      \
                          ▼      ▼       ▼
                    Notification Audit Analytics
```

Inside the backend:

``` text
Controller
    ↓
Service
    ↓
Domain Logic
    ↓
Repository
    ↓
PostgreSQL
```

With:

``` text
SOLID
+
DI
+
Strategy
+
Repository
+
State
+
Adapter
+
Outbox
+
Idempotency
+
ACID
+
Concurrency Control
```

------------------------------------------------------------------------

# 43. Final Design Philosophy

The strongest answer to the challenge is not:

> "We used microservices because the system may have 10 million users."

The stronger answer is:

> "We designed a correct financial core first. The API layer is
> stateless so it can scale horizontally behind a load balancer.
> Financial state is protected using ACID transactions, database-level
> concurrency control, constraints and idempotency. Non-critical
> operations such as notifications and analytics are asynchronous
> through an event-driven mechanism. As the dataset and traffic grow,
> read replicas and caching can be introduced, followed by carefully
> designed partitioning/sharding when required."

That demonstrates actual engineering judgment.

------------------------------------------------------------------------

# 44. One-Minute Judge Explanation

If a judge asks:

**"Why is your system scalable and reliable?"**

Answer:

> Our financial core uses a relational database with ACID transactions
> because correctness is the most important requirement for money
> movement. We use idempotency keys and database constraints to prevent
> duplicate transfers, while concurrency control prevents race
> conditions when multiple requests modify the same account
> simultaneously. The backend is stateless, so multiple instances can
> run behind a load balancer for horizontal scaling. We keep
> notifications, analytics and other non-critical work asynchronous
> using an outbox/event mechanism. Read replicas and caching can reduce
> database load, while database sharding is reserved for a future stage
> when the data volume actually requires it. SOLID principles and
> patterns such as Strategy, Repository, Adapter, State and Dependency
> Injection keep the code maintainable.

------------------------------------------------------------------------

# 45. Final Checklist

Before submission, verify:

-   [ ] User registration
-   [ ] BDT 100,000 initial balance
-   [ ] Login/authentication
-   [ ] Send money
-   [ ] Request money
-   [ ] Accept/reject request
-   [ ] Transaction history
-   [ ] ACID transfer
-   [ ] Concurrency protection
-   [ ] Idempotency
-   [ ] Unique transaction reference
-   [ ] Ledger/audit trail
-   [ ] Server-side validation
-   [ ] Stateless backend
-   [ ] Rate limiting
-   [ ] Async notification
-   [ ] Outbox/event mechanism
-   [ ] SOLID principles
-   [ ] At least 2--4 meaningful design patterns
-   [ ] Error handling
-   [ ] Unit tests
-   [ ] Integration tests
-   [ ] Concurrency tests
-   [ ] Load test/demo
-   [ ] Architecture diagram
-   [ ] Database ER diagram
-   [ ] Sequence diagram for transfer
-   [ ] Failure scenario explanation
-   [ ] Scalability roadmap

------------------------------------------------------------------------

# 46. Most Important Priorities

If time becomes very limited, prioritize these:

``` text
                    ┌──────────────────┐
                    │ CORRECT MONEY    │
                    │    MOVEMENT      │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
           ACID         Concurrency     Idempotency
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                       Ledger / Audit
                             │
                             ▼
                    Stateless Backend
                             │
                             ▼
                      Load Balancer
                             │
                             ▼
                    Async Processing
                             │
                             ▼
                Future Replication/Sharding
```

**Priority order:**

1.  Correctness
2.  Concurrency
3.  Idempotency
4.  Security
5.  Reliability
6.  Maintainability
7.  Scalability
8.  Advanced distributed architecture

Do not sacrifice the first five merely to claim scalability.
