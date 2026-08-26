You are a senior backend architect, Python/FastAPI engineer, database engineer and payments-integration engineer.

You have access to two important project resources:

1. The uploaded FlyRank Internship PDF:
   "Usage Metering & Billing Engine Live Capstone.pdf"

2. The uploaded Agentic Awesome Skills repository:
   "agentic-awesome-skills"

The PDF is the authoritative project specification.

The skills repository is a toolbox. Do NOT blindly activate every skill.

==================================================
PROJECT OBJECTIVE
==================================================

Build the Usage Metering & Billing Engine described in the uploaded PDF.

This is a correctness-first SaaS backend that answers:

1. How much has a tenant used?
2. How much should that usage cost?
3. Has the tenant reached its plan quota?

The system must support:

- multi-tenant usage metering
- idempotent usage recording
- quota enforcement
- API-call usage
- simulated AI-token usage
- accurate money calculations
- Stripe test-mode subscriptions
- Stripe Checkout
- signed webhook verification
- webhook deduplication
- subscription/plan synchronization
- monthly usage rollups
- evidence/documentation
- automated tests

IMPORTANT:

The PDF explicitly states that AI is NOT required for the core project.

Do not introduce an LLM, RAG system, vector database, AI agent or Gemini API into the core architecture.

AI-token usage should be simulated as numeric usage data.

==================================================
TECHNOLOGY
==================================================

Prefer:

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Alembic
- Docker Compose
- Stripe test mode
- Stripe CLI
- pytest

Use additional technologies only when they provide a clear engineering benefit.

Keep the system small and production-oriented.

==================================================
SKILL SELECTION
==================================================

Before implementing anything, inspect the uploaded Agentic Awesome Skills repository.

Use the catalog to identify the exact current skill IDs.

Do NOT load all skills.

Select only the skills relevant to the current project.

At minimum investigate these skills:

- @brainstorming
- @backend-architect
- @api-design-principles
- @fastapi-pro
- @database-architect
- @postgresql
- @stripe-integration
- @auth-implementation-patterns
- @api-security-best-practices
- @python-testing-patterns
- @test-driven-development
- @debugging-strategies
- @observability-engineer
- @pre-release-review
- @production-audit

If the repository contains more specialized skills for:
- idempotency
- webhook reliability
- financial calculations
- billing
- concurrency
- transaction safety

prefer those specialized skills where appropriate.

For each selected skill:

1. Give its exact skill ID.
2. Explain why it is relevant.
3. State which project phase uses it.
4. Load/read its SKILL.md before relying on it.

Do not use skills unrelated to this backend billing project.

==================================================
PHASE 1 — REQUIREMENT ANALYSIS
==================================================

Read the entire PDF before writing code.

Extract the exact requirements.

Create a requirements matrix:

Requirement
→ PDF section/page
→ implementation module
→ API endpoint
→ database component
→ test
→ EVIDENCE.md proof

Pay particular attention to:

- exactly-once metering
- idempotency
- quota boundaries
- 429 vs 402
- money stored as integers
- AI token pricing
- cached input tokens
- reasoning tokens
- Stripe Checkout
- webhook signatures
- duplicate webhook events
- tenant isolation
- required repository files
- acceptance probes

Do not silently invent requirements.

==================================================
PHASE 2 — ARCHITECTURE
==================================================

Design a layered architecture:

HTTP/API layer
↓
Application/service layer
↓
Domain/business logic
↓
Repository/data-access layer
↓
PostgreSQL

Separate:

- HTTP concerns
- business logic
- persistence
- Stripe integration
- billing calculations
- metering
- quota enforcement

Do not create unnecessary microservices.

Keep this as a modular monolith unless the PDF explicitly requires otherwise.

==================================================
PHASE 3 — DATABASE DESIGN
==================================================

Design the PostgreSQL schema for at least:

- tenants
- plans
- subscriptions
- usage_events
- webhook_events

Add users/authentication tables if required by the chosen authentication design.

Define:

- primary keys
- foreign keys
- unique constraints
- indexes
- timestamps
- tenant isolation rules

CRITICAL:

usage_events must prevent duplicate billing through database-level idempotency.

Design a unique constraint around:

tenant_id + idempotency_key

Do not rely only on application-level duplicate checking.

Also design webhook_events so the same Stripe event cannot be processed twice.

==================================================
PHASE 4 — METERING
==================================================

Implement:

MeterService.record(
    tenant,
    usage_type,
    quantity,
    idempotency_key
)

Required behavior:

First request:

idempotency key = abc123
→ create usage event
→ return result

Retry:

idempotency key = abc123
→ detect existing event
→ return original result
→ DO NOT create another event

Handle concurrent requests safely.

Think carefully about:

- transactions
- unique constraints
- race conditions
- isolation
- retries
- rollback behavior

Write tests proving exactly-once behavior.

==================================================
PHASE 5 — QUOTA ENFORCEMENT
==================================================

Before allowing a billable operation:

current usage + requested usage
→ compare with plan limit
→ allow OR reject

Support:

API calls
AI tokens

Implement exact boundary behavior.

Explicitly test:

usage = limit - 1
request = 1

usage = limit
request = 1

usage = limit - 10
request = 10

usage = limit - 10
request = 11

Use the documented semantics:

429 Too Many Requests
→ usage quota exceeded

402 Payment Required
→ upgrade/payment required

Messages must clearly explain why the request was blocked.

Do not use status codes inconsistently.

==================================================
PHASE 6 — COST CALCULATION
==================================================

Implement money calculations using integers.

NEVER use floating point for monetary values.

Use cents or another integer micro-unit representation.

Implement API-call pricing.

Implement AI-token pricing according to the PDF:

input tokens
cached input tokens
output tokens
reasoning tokens

Important rules:

- cached input tokens are cheaper
- reasoning tokens count as output tokens
- token categories must not be incorrectly double-counted
- pricing constants must be pinned in configuration

Create deterministic tests for all pricing categories.

The calculator must produce exact expected totals.

==================================================
PHASE 7 — STRIPE
==================================================

Implement Stripe TEST MODE only.

Never use live Stripe mode.

Implement:

1. Stripe customer handling
2. Checkout session
3. Pro subscription
4. checkout.session.completed
5. customer.subscription.updated
6. customer.subscription.deleted

Webhook flow:

Stripe
→ raw request body
→ verify signature
→ deduplicate event
→ process event
→ update tenant subscription
→ update plan/status

CRITICAL:

Signature verification must happen before processing.

Invalid signature:

→ HTTP 400
→ database unchanged

Duplicate valid event:

→ ignore/replay safely
→ process only once

Never trust client-provided subscription state.

Stripe is the payment source of truth.

Your database mirrors Stripe state through verified webhook events.

==================================================
PHASE 8 — API DESIGN
==================================================

Design a clean REST API.

At minimum consider:

POST /generate

GET /usage

POST /checkout

POST /webhooks/stripe

GET /health

Add other endpoints only when justified.

For every endpoint define:

- request schema
- response schema
- authentication
- validation
- errors
- status codes
- idempotency behavior
- database interactions

Document the API.

==================================================
PHASE 9 — BACKGROUND JOB
==================================================

The PDF requires at least one background job.

Implement a meaningful background operation rather than adding a fake job.

Possible example:

- periodic usage aggregation
- reconciliation
- webhook cleanup
- usage rollup

The job must include:

- retry behavior
- failure handling
- idempotency where needed
- logging
- clear responsibility

Keep the core request path synchronous where practical.

==================================================
PHASE 10 — SECURITY
==================================================

Apply:

- authentication
- authorization
- tenant isolation
- input validation
- rate limiting where appropriate
- secure secrets management
- Stripe webhook signature verification
- safe error responses
- no secrets in logs
- no secrets committed to Git
- .env in .gitignore
- .env.example with placeholders

Never expose Stripe secret keys.

Never trust tenant IDs supplied by unauthorized clients.

Every tenant-specific query must enforce tenant isolation.

==================================================
PHASE 11 — TESTING
==================================================

Testing is extremely important because this is a billing system.

Create:

Unit tests
Integration tests
API tests
Database tests
Stripe webhook tests

Especially test:

1. Same idempotency key twice
2. Same request concurrently
3. Different idempotency keys
4. Quota at 999/1000
5. Quota exactly at 1000
6. Quota exceeded
7. 429 response
8. 402 response
9. Cached token pricing
10. Reasoning token pricing
11. Combined token pricing
12. Integer money calculations
13. Stripe Checkout
14. Valid webhook
15. Invalid webhook signature
16. Duplicate webhook
17. Subscription upgrade
18. Subscription cancellation
19. Tenant isolation
20. Database rollback
21. Webhook processing failure
22. Retry behavior

==================================================
PHASE 12 — REQUIRED REPOSITORY FILES
==================================================

The final repository MUST contain:

README.md
capstone.yaml
EVIDENCE.md
BUILDLOG.md
.env.example

Also create:

.gitignore

The README must include:

- project overview
- architecture diagram
- setup instructions
- Docker instructions
- database setup
- seed instructions
- Stripe CLI setup
- endpoint documentation
- test instructions
- limitations

EVIDENCE.md must contain proof for every PDF requirement.

BUILDLOG.md must honestly record:

- where AI helped
- where AI produced incorrect output
- what was changed
- important engineering decisions

Do not fabricate evidence.

==================================================
PHASE 13 — ACCEPTANCE PROBES
==================================================

Build the system so these probes pass.

PROBE 1:

Send the same billable request twice with the same idempotency key.

Expected:

Exactly one usage event.

Second response mirrors the first.

PROBE 2:

Drive a tenant to the exact quota.

The boundary request behaves according to documented rules.

The next request returns:

429 or 402

with a clear explanation.

PROBE 3:

Complete Stripe test Checkout.

Expected:

Free → Pro

GET /usage shows new limits.

PROBE 4:

Send forged webhook.

Expected:

400

No state change.

Then replay a valid event.

Expected:

Processed once.

PROBE 5:

Verify token pricing.

Cached input and reasoning token rules must produce exact expected totals.

GET /usage must match the pinned pricing configuration.

==================================================
PHASE 14 — DOCUMENTATION
==================================================

Generate an architecture diagram.

Document important invariants such as:

INVARIANT 1:
One idempotency key can produce at most one usage event per tenant.

INVARIANT 2:
A billable operation cannot exceed its quota.

INVARIANT 3:
Money is never represented using floating point.

INVARIANT 4:
Unverified Stripe events never mutate billing state.

INVARIANT 5:
One Stripe event ID is processed at most once.

INVARIANT 6:
A tenant cannot access another tenant's usage or subscription data.

INVARIANT 7:
Pricing is deterministic from pinned configuration.

==================================================
IMPLEMENTATION RULE
==================================================

DO NOT build everything in one operation.

Work phase-by-phase.

For each phase:

1. Inspect existing code.
2. Identify relevant skills.
3. Read the relevant SKILL.md files.
4. Implement.
5. Run tests.
6. Fix failures.
7. Review the implementation.
8. Update documentation.
9. Update EVIDENCE.md where appropriate.
10. Update BUILDLOG.md.
11. Commit with a meaningful Git commit message.
12. Move to the next phase.

Do not claim a feature is complete without verification.

==================================================
IMPORTANT ENGINEERING PRINCIPLES
==================================================

Correctness > feature count.

Do not over-engineer.

Do not create microservices unnecessarily.

Do not introduce AI into the core.

Do not use floats for money.

Do not trust client billing state.

Do not process Stripe webhooks without signature verification.

Do not rely only on application-level idempotency.

Use database constraints to enforce correctness.

Do not hide errors.

Do not fabricate evidence.

Do not commit secrets.

Every important requirement should have a reproducible proof.

At the end, run a final review against every requirement in the uploaded PDF.

STOP after Phase 1 architecture/design and show me:

1. Requirements matrix
2. Exact selected skills
3. Architecture
4. Database schema
5. API contract
6. Idempotency strategy
7. Quota strategy
8. Cost calculation design
9. Stripe webhook design
10. Testing strategy
11. Repository structure
12. Implementation roadmap

Do not write the implementation yet.
Wait for approval before proceeding.