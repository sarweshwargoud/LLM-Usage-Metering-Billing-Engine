# Build Log — LLM Usage Metering & Billing Engine

## Phase 1: Database Foundation
- Implemented PostgreSQL schema using SQLAlchemy 2.0 and Alembic migrations (`0001_initial_schema.py`).
- Created tables: `tenants`, `plans`, `subscriptions`, `usage_events`, `quota_counters`, `webhook_events`, `monthly_usage_rollups`, `job_runs`.
- Enforced all critical invariants at database level:
  - `UNIQUE(tenant_id, idempotency_key)`
  - `CHECK(quantity BETWEEN 1 AND 1000000)`
  - `CHECK(used <= plan_limit)`
  - `BIGINT` for all quantities, counters, and micro-cents amounts (zero floats).
  - Out-of-order webhook protection timestamp column (`last_event_timestamp`).
  - Period-aware quota composite primary key `(tenant_id, usage_type, billing_period_start)`.
- Verified live PostgreSQL Docker test database with 41 constraint tests passing.

---

## Phase 2: Service Layer
- **Domain Layer (`app/domain/`):**
  - Implemented immutable `TokenUsage` with strict invariant validation (`cached <= input`, `reasoning <= output`, non-negative integer checks).
  - Implemented exception hierarchy: `MeteringError`, `InvalidUsageError`, `QuotaExceededError`, `IdempotencyConflictError`, `TenantNotFoundError`.
- **Services (`app/services/`):**
  - `TokenCostCalculator`: Exact `Decimal` arithmetic for uncached input, cached input, output, reasoning tokens, and API calls with `ROUND_HALF_UP` rounding to integer micro-cents.
  - `QuotaEnforcer`: TOCTOU-free atomic quota enforcement via single-statement `UPDATE ... RETURNING` and concurrent-safe counter auto-initialization via `INSERT ... ON CONFLICT DO NOTHING`.
  - `MeterService`: Canonical SHA-256 request payload hashing, exact retry replay (returning cached response without re-charging), 422 payload mismatch rejection (`IdempotencyConflictError`), and live multi-threaded race collision recovery without 500 errors.
- **Testing & Verification:**
  - Added 36 tests across `test_token_cost_calculator.py`, `test_quota_enforcer.py`, and `test_meter_service.py`.
  - Verified multi-threaded concurrency tests against real PostgreSQL Docker instance.
  - Total test suite: 77/77 passed (41 Phase 1 + 36 Phase 2).

---

## Phase 3: FastAPI API + Authentication + Tenant Isolation
- **Authentication & Security (`app/auth/`):**
  - Cryptographic JWT signing and decoding with `HS256`, 24h expiration, and issuer verification.
  - Implemented `get_current_tenant` dependency enforcing the invariant: **Client cannot choose or spoof `tenant_id`**.
- **Schemas (`app/schemas/`):**
  - Implemented `GenerateRequest`, `GenerateResponse`, `TokenUsageSchema`, `UsageSummaryResponse`, `HealthResponse`.
  - Pydantic models validate invariant bounds (`cached <= input`, `reasoning <= output`, `quantity BETWEEN 1 AND 1000000`).
- **API Routes & Global Error Mapping (`app/api/`):**
  - `POST /generate`: Supports HTTP `Idempotency-Key` header with body fallback; delegates directly to `MeterService`.
  - `GET /usage`: Queries authenticated tenant's period quota and usage aggregates.
  - `GET /health`: Safe service readiness and database connectivity ping.
  - `POST /auth/token`: Development and testing JWT issuance.
  - Clean HTTP error mappings: `QuotaExceededError` $\rightarrow$ `429`, `IdempotencyConflictError` $\rightarrow$ `422`, `InvalidUsageError` $\rightarrow$ `422`, Auth errors $\rightarrow$ `401`.
- **Testing & Verification:**
  - Added 28 tests in `tests/test_api/` covering JWT authentication, cross-tenant isolation, generation idempotency, quota overruns, CORS pre-flight, and security attack vectors.
  - Total test suite: 105/105 passed (41 Phase 1 + 36 Phase 2 + 28 Phase 3).

---

## Phase 3: Adversarial Security & API Review Findings & Hardening
1. **CORS Hardening**: Changed CORS configuration to `allow_credentials=False` when wildcard origins `["*"]` are configured, preventing insecure credential exposure.
2. **Idempotency Disagreement Hardening**: Added explicit mismatch validation between HTTP `Idempotency-Key` header and request body `idempotency_key` (rejecting with 422 if both are provided with conflicting values).
3. **Information Leakage Prevention**: Sanitized 401/422/500 error messages to suppress raw Python exception strings, SQL queries, and internal stack traces.
4. **Token Issuance Model Documented**: Documented that `/auth/token` functions as a development/test helper token issuer. In production, this would be backed by password hashing / OAuth2 / API key verification.
5. **Rate Limiting Distinction**: Verified that plan-based billing quotas are enforced via `QuotaEnforcer` without conflating them with HTTP network-level rate limiters.

---

## Phase 4: Stripe Checkout + Subscription Integration
- **Stripe Service (`app/services/stripe_service.py`):**
  - Implemented `get_or_create_customer`: uses `SELECT FOR UPDATE` and deterministic `idempotency_key=f"create_customer_{tenant_id}"` on Stripe Customer creation to prevent race-condition duplicates.
  - Implemented `create_checkout_session`: resolves server-pinned Stripe Price IDs (`stripe_price_pro`), configures `mode="subscription"`, passes safe metadata (`tenant_id`, `plan`), namespaces idempotency keys by `tenant_id` (`f"checkout_{tenant_id}_{plan}_{key}"`) to prevent cross-tenant key collision in Stripe, and generates hosted Stripe Checkout URLs.
  - Enforced zero false activation: checkout creation does NOT insert or activate database subscription records (subscription activation is strictly reserved for Phase 5 webhooks).
- **API Endpoint (`POST /billing/checkout`):**
  - Authenticated via JWT `get_current_tenant`.
  - Accepts internal plan slug (`pro`) and rejects arbitrary client price IDs.
  - Handles Stripe API exceptions cleanly with 502/400 without leaking secrets or connection details.
- **Testing & Verification:**
  - Added 9 tests in `tests/test_stripe/test_billing_checkout.py` covering customer creation/reuse, parameter validation, URL config, safe error handling, tenant isolation, cross-tenant idempotency namespacing, and subscription dormancy before webhook.
  - Total test suite: 114/114 passed (41 Phase 1 + 36 Phase 2 + 28 Phase 3 + 9 Phase 4).

---

## Phase 4: Adversarial Correctness Review Findings & Hardening
1. **Cross-Tenant Idempotency Key Collision**:
   - *Vulnerability*: Passing client-supplied `Idempotency-Key` raw to Stripe would allow Tenant B to collide with Tenant A's account-wide Stripe key.
   - *Fix*: Namespaced all Stripe idempotency keys with `tenant_id`: `f"checkout_{tenant_id}_{plan}_{idempotency_key}"`.
2. **Concurrent Customer Creation Race**:
   - *Vulnerability*: Two concurrent requests for a fresh tenant could both invoke `stripe.Customer.create()`.
   - *Fix*: Added PostgreSQL row locking (`SELECT FOR UPDATE`) and Stripe Customer creation idempotency key (`create_customer_{tenant_id}`).
3. **Dual-System (PostgreSQL + Stripe) Consistency**:
   - Analyzed failure window points A through G. Proved that deterministic customer and checkout idempotency keys allow clean retries across process crashes or commit failures without orphan duplicate records.

---

## Phase 5: Stripe Webhook Ingestion + Subscription Lifecycle + Monthly Rollups
- **Webhook Service (`app/services/webhook_service.py`):**
  - Cryptographic HMAC-SHA256 signature verification via `stripe.Webhook.construct_event`.
  - Idempotent deduplication against `webhook_events(stripe_event_id)` primary key, returning `already_processed` on duplicate deliveries.
  - Three-state state machine: `processing` $\rightarrow$ `processed` / `failed`, with stale lock detection (> 5 minutes).
  - Out-of-Order Resiliency: verifies `event.created > subscriptions.last_event_timestamp` (rejecting stale and equal timestamp events) to prevent asynchronous delivery anomalies from reverting active subscription states.
  - Tenant-Customer Integrity: Cross-verifies `customer_id` and metadata `tenant_id`, rejecting conflicting tenant attributions.
  - Transaction Isolation: Wraps event dispatch in a database `SAVEPOINT` so that dispatch failures roll back subscription mutations while saving `WebhookEvent.status = 'failed'`.
- **Monthly Rollup Service (`app/services/rollup_service.py`):**
  - Periodic background aggregation of `usage_events` into `monthly_usage_rollups`.
  - Native atomic PostgreSQL upsert (`ON CONFLICT (tenant_id, billing_period_start, usage_type) DO UPDATE ...`).
  - Idempotent execution across multiple runs and incremental usage additions.
- **Testing & Verification:**
  - Added 10 tests across `tests/test_webhooks/` and `tests/test_jobs/` verifying signature validation, duplicate replay, equal timestamp guards, tenant mismatch detection, incremental rollup aggregation, and month boundary isolation.
  - Total test suite: 124/124 passed (41 Phase 1 + 36 Phase 2 + 28 Phase 3 + 9 Phase 4 + 10 Phase 5).

---

## Phase 6: Background Jobs + Production Integration + Acceptance Hardening
- **Background Job Runner (`app/services/job_runner.py`):**
  - Concurrency Locking: Enforces single-worker job execution per billing period via PostgreSQL partial unique index `uix_job_runs_active_job` (`WHERE status = 'running'`).
  - Crash Recovery: Automatically detects and reclaims stale running jobs older than 15 minutes, transitioning them to `failed` and allowing clean retries.
  - Safe Retry & Attempt Tracking: Increments `attempt_number` on subsequent executions while preserving error tracebacks in `job_runs.failure_detail`.
  - Transactional Isolation: Encloses rollup calculations within a dedicated savepoint so that compute failures revert dirty aggregates while recording `status = 'failed'`.
- **End-to-End Billing Lifecycle Integration (`tests/test_e2e/test_complete_billing_flow.py`):**
  - Verified full user journey: Tenant provision $\rightarrow$ JWT issue $\rightarrow$ POST /generate (token usage, cache pricing, reasoning tokens, atomic quota deduction) $\rightarrow$ POST /billing/checkout (customer creation & hosted session) $\rightarrow$ Stripe webhook (active Pro plan upgrade) $\rightarrow$ Background Monthly Rollup Job execution $\rightarrow$ Database rollup aggregation verification.
- **Testing & Final Verification:**
  - Total test suite: 129/129 passed across all 6 phases with 0 failures on live Docker PostgreSQL 16.15.
