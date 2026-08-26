# LLM Usage Metering & Billing Engine

Production-grade, highly reliable usage metering, quota enforcement, and billing engine for LLM APIs built with **FastAPI**, **PostgreSQL**, **SQLAlchemy**, and **Stripe**.

---

## Architecture & System Overview

```
Client Request (JWT + Idempotency-Key)
           │
           ▼
FastAPI Gateway (Auth + CORS + Schema Validation)
           │
           ▼
MeterService ──► TokenCostCalculator (Decimal / Micro-cents)
           │
           ▼
QuotaEnforcer ──► PostgreSQL (Atomic TOCTOU-free UPDATE)
           │
           ▼
Stripe Service ──► Hosted Checkout Sessions / Webhooks
           │
           ▼
JobRunner ──► Idempotent Monthly Usage Rollups (BIGINT Aggregates)
```

---

## Key Features

- **Exact & Deterministic Token Pricing**: Supports normal input, cached input, output, and reasoning tokens using integer micro-cents (`$0.000001` precision) and `ROUND_HALF_UP` arithmetic.
- **TOCTOU-Free Atomic Quota Enforcement**: Single-statement PostgreSQL atomic updates with period-aware composite keys `(tenant_id, usage_type, billing_period_start)`.
- **Cryptographic Idempotency & Replay**: Request canonicalization with SHA-256 payload hashing. Replays exact stored responses for duplicate requests; rejects tampered payloads on same key with `422 Unprocessable Entity`.
- **Stripe Checkout & Subscription Lifecycle**: Seamless upgrade flows with tenant-scoped idempotency keys, Stripe customer reuse, and HMAC-verified webhooks (`customer.subscription.*`, `invoice.*`).
- **Out-of-Order Webhook Protection**: Enforces timestamp comparison against `subscriptions.last_event_timestamp` to prevent stale asynchronous deliveries from corrupting active subscription states.
- **Background Job Coordination & Crash Recovery**: `JobRunner` enforces single-worker execution locks via PostgreSQL partial unique index `uix_job_runs_active_job` and auto-reclaims crashed jobs older than 15 minutes.
- **Strict Tenant Isolation**: JWT-based authentication ensuring tenant identity is cryptographically derived from verified tokens (`sub`).

---

## Local Setup & Quickstart Guide

### Prerequisites
- **Python 3.10+**
- **Docker Desktop** (for PostgreSQL)
- **Git**

---

### Step 1: Clone the Repository
```bash
git clone https://github.com/sarweshwargoud/LLM-Usage-Metering-Billing-Engine.git
cd LLM-Usage-Metering-Billing-Engine
```

---

### Step 2: Set Up Python Virtual Environment

**On Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**On Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

---

### Step 3: Install Dependencies
```bash
pip install --upgrade pip
pip install -e .
pip install pytest pytest-asyncio
```

---

### Step 4: Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
*(Windows PowerShell: `Copy-Item .env.example .env`)*

Configure your environment settings in `.env`:
```env
SECRET_KEY=generate_with_openssl_rand_hex_32
ENVIRONMENT=development
DATABASE_URL_SYNC=postgresql+psycopg2://billing_user:billing_secret@localhost:5432/billing_engine
TEST_DATABASE_URL_SYNC=postgresql+psycopg2://billing_test:billing_test_secret@localhost:5433/billing_engine_test
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO=price_1QtestProPlan000000
```

---

### Step 5: Start PostgreSQL via Docker Compose
Start the primary database (port 5432) and test database (port 5433):
```bash
docker compose up -d
```

Verify containers are running and healthy:
```bash
docker compose ps
```

---

### Step 6: Apply Database Migrations (Alembic)
Run Alembic migrations to apply the latest database schema:
```bash
python -m alembic upgrade head
```

---

### Step 7: Run the FastAPI Server
Start the development server with live reload:
```bash
uvicorn app.main:app --reload --port 8000
```

The interactive Swagger API documentation is available at:
👉 **`http://localhost:8000/docs`**

---

### Step 8: Run the Complete Test Suite
Run the 129+ test suite against the live Docker PostgreSQL test instance:

**Windows PowerShell:**
```powershell
$env:TEST_DATABASE_URL_SYNC="postgresql+psycopg2://billing_test:billing_test_secret@localhost:5433/billing_engine_test"
$env:ENVIRONMENT="testing"
python -m pytest -v
```

**Linux / macOS:**
```bash
TEST_DATABASE_URL_SYNC="postgresql+psycopg2://billing_test:billing_test_secret@localhost:5433/billing_engine_test" ENVIRONMENT="testing" python -m pytest -v
```

---

## API Reference

### 1. Health Ping
`GET /health`
```json
{
  "status": "healthy",
  "database": "connected"
}
```

### 2. Tenant Token Minting (Development)
`POST /auth/token`
```json
{
  "email": "developer@acme.com"
}
```
**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6...",
  "token_type": "bearer",
  "tenant_id": "8f3b1451-93c6-43b8-a681-7f912e75e119",
  "expires_in": 86400
}
```

### 3. Metered Generation Request
`POST /generate`  
*Headers: `Authorization: Bearer <JWT>`, `Idempotency-Key: <UUID>`*
```json
{
  "prompt": "Summarize technical architecture",
  "model": "gemini-1.5-pro",
  "usage_type": "token",
  "token_usage": {
    "input_tokens": 1500,
    "output_tokens": 500,
    "cached_input_tokens": 300,
    "reasoning_tokens": 100
  }
}
```
**Response:**
```json
{
  "event_id": "c1f7b8e1-5cb1-4f3b-8219-c68912e4f012",
  "tenant_id": "8f3b1451-93c6-43b8-a681-7f912e75e119",
  "usage_type": "token",
  "quantity": 2000,
  "cost_micro_cents": 502500,
  "quota_used": 2000,
  "quota_limit": 100000,
  "quota_remaining": 98000
}
```

### 4. Stripe Checkout Session Initiation
`POST /billing/checkout`  
*Headers: `Authorization: Bearer <JWT>`, `Idempotency-Key: <UUID>`*
```json
{
  "plan": "pro"
}
```
**Response:**
```json
{
  "checkout_url": "https://checkout.stripe.com/pay/cs_test_a1b2c3d4",
  "session_id": "cs_test_a1b2c3d4",
  "plan": "pro",
  "stripe_customer_id": "cus_N982aXbc123"
}
```

### 5. Stripe Webhook Ingestion
`POST /billing/webhook`  
*Headers: `Stripe-Signature: t=...,v1=...`*

Receives and processes signed Stripe events:
- `customer.subscription.created` / `updated` $\rightarrow$ Activates Pro plan
- `customer.subscription.deleted` $\rightarrow$ Sets status to cancelled and records `cancelled_at`
- `invoice.payment_succeeded` / `failed` $\rightarrow$ Updates subscription status

### 6. Tenant Usage Summary
`GET /usage`  
*Headers: `Authorization: Bearer <JWT>`*
```json
{
  "tenant_id": "8f3b1451-93c6-43b8-a681-7f912e75e119",
  "plan": "pro",
  "billing_period_start": "2026-08-01",
  "usage": {
    "token": {
      "used": 3500,
      "plan_limit": 10000000,
      "remaining": 9996500,
      "total_cost_micro_cents": 862500
    },
    "api_call": {
      "used": 12,
      "plan_limit": 100000,
      "remaining": 99988,
      "total_cost_micro_cents": 1200
    }
  }
}
```

---

## Background Monthly Usage Rollup Job

Execute the background rollup aggregation job programmatically or via cron:
```python
from app.database import get_db_context
from app.services.job_runner import JobRunner
from datetime import date

with get_db_context() as session:
    runner = JobRunner()
    result = runner.run_monthly_rollup_job(session, billing_period=date(2026, 8, 1))
    print(result)
```

---

## Project Structure

```
.
├── alembic/                 # Database migrations (0001_initial_schema)
├── app/
│   ├── api/                 # FastAPI routes & error handlers (v1)
│   ├── auth/                # JWT cryptography & dependency injection
│   ├── domain/              # TokenUsage value object & domain exceptions
│   ├── models/              # SQLAlchemy ORM models (BigInt, Constraints)
│   ├── schemas/             # Pydantic request/response validation
│   ├── services/            # MeterService, QuotaEnforcer, StripeService, JobRunner
│   ├── config.py            # Pydantic Settings & environment validation
│   ├── database.py          # Session engines & connection pooling
│   └── main.py              # FastAPI app initialization & CORS middleware
├── docker-compose.yml       # Multi-container PostgreSQL setup (dev & test)
├── pyproject.toml           # Project dependencies & packaging
└── tests/                   # 129+ automated tests
    ├── test_api/            # FastAPI endpoint & auth tests
    ├── test_db/             # Database constraints & concurrency tests
    ├── test_e2e/            # Complete billing lifecycle E2E tests
    ├── test_jobs/           # JobRunner & monthly rollup tests
    ├── test_services/       # Service unit & PostgreSQL concurrency tests
    ├── test_stripe/         # Stripe checkout tests
    └── test_webhooks/       # Stripe webhook signature & state machine tests
```

---

## License
MIT
