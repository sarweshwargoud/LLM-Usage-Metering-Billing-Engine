# Evidence of Correctness — Phase 2 Service Layer

This document contains reproducible test execution evidence for the core correctness guarantees of the LLM Usage Metering & Billing Engine.

---

## 1. Test Suite Execution Summary

```
============================= test session starts =============================
platform win32 -- Python 3.10.9, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\sarwe\Desktop\Projects\LLM-Usage Metering & Billing Engine
configfile: pyproject.toml
plugins: anyio-4.12.1, asyncio-1.4.0
collected 129 items

tests/test_api/test_auth.py .......                                              [  5%]
tests/test_api/test_generate.py ......                                            [ 10%]
tests/test_api/test_security_adversarial.py ..........                            [ 17%]
tests/test_api/test_tenant_isolation.py ...                                       [ 20%]
tests/test_api/test_usage_and_health.py ..                                        [ 21%]
tests/test_db/test_schema_constraints.py ......................................... [ 53%]
tests/test_e2e/test_complete_billing_flow.py .                                    [ 54%]
tests/test_jobs/test_job_runner.py ....                                           [ 57%]
tests/test_jobs/test_monthly_rollups.py ..                                        [ 58%]
tests/test_services/test_meter_service.py ...........                             [ 67%]
tests/test_services/test_quota_enforcer.py ........                                [ 73%]
tests/test_services/test_token_cost_calculator.py .................                [ 86%]
tests/test_stripe/test_billing_checkout.py .........                               [ 93%]
tests/test_webhooks/test_stripe_webhooks.py ........                              [100%]

====================== 129 passed, 2 warnings in 3.71s ========================
```

---

## 2. Evidence of Key Invariants

### Invariant 1: Exactly-Once Metering & Idempotent Replay (Cases A & B)
- **Test:** `tests/test_services/test_meter_service.py::TestMeterServiceIdempotency::test_case_b_exact_retry_returns_original_response_without_recharging`
- **Result:** Calling `record_usage` twice with the same key and payload returns the original stored `response_payload`.
- **Database Verification:**
  - `usage_events` count = `1`
  - `quota_counters.used` = `15` (deducted exactly once, not 30)

### Invariant 2: Payload Mismatch Detection & 422 Conflict (Case C)
- **Test:** `tests/test_services/test_meter_service.py::TestMeterServiceIdempotency::test_case_c_same_key_different_payload_raises_conflict`
- **Result:** Presenting an existing idempotency key with modified request parameters immediately raises `IdempotencyConflictError`.
- **Database Verification:** Quota is not deducted for the tampered request; no secondary event is created.

### Invariant 3: TOCTOU Elimination & Atomic Quota Enforcement
- **Test:** `tests/test_services/test_quota_enforcer.py::TestQuotaConcurrencyPostgreSQL::test_concurrent_quota_requests_never_overrun_limit`
- **Scenario:** Plan limit = 100. 10 simultaneous threads compete to deduct 20 units each (total 200 units requested).
- **Result:** Exactly 5 threads succeed (100 units consumed). Exactly 5 threads fail with `QuotaExceededError`.
- **Database Verification:** `quota_counters.used` = `100` (never 120 or 200).

### Invariant 4: Concurrent Same-Key Race Collision Recovery
- **Test:** `tests/test_services/test_meter_service.py::TestMeterServiceConcurrencyPostgreSQL::test_concurrent_same_key_requests_exactly_once`
- **Scenario:** 8 simultaneous threads submit identical requests with the same idempotency key.
- **Result:** All 8 threads receive valid HTTP/service responses with identical event IDs (0 unhandled exceptions / 500 errors).
- **Database Verification:** Exactly 1 `usage_events` row created, quota incremented only once (`used = 10`, not `80`).

### Invariant 5: Exact Decimal Arithmetic & Zero-Float Pricing
- **Test:** `tests/test_services/test_token_cost_calculator.py::TestTokenPricingCalculations`
- **Verification:**
  - Normal input (10,000 tokens @ $0.15/1M) = 1,500,000 micro-cents
  - Cached input (10,000 tokens @ $0.075/1M) = 750,000 micro-cents
  - Output tokens (5,000 tokens @ $0.60/1M) = 3,000,000 micro-cents
  - Reasoning tokens billed at output rate without double billing.
  - Deterministic `ROUND_HALF_UP` rounding to integer micro-cents.
