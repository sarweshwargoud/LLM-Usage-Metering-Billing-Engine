#!/usr/bin/env python3
"""
Phase 1 Database Setup & Test Runner.

Run this script AFTER starting the Docker services:
    docker compose up -d

Usage:
    python scripts/run_phase1_tests.py
"""
import os
import subprocess
import sys


def run(cmd: list[str], **kwargs) -> int:
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    return result.returncode


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)

    # 1. Wait for test DB to be ready
    print("=" * 60)
    print("STEP 1: Waiting for test database...")
    import time
    import socket
    for attempt in range(30):
        try:
            s = socket.create_connection(("localhost", 5433), timeout=2)
            s.close()
            print("Test DB (port 5433) is ready.")
            break
        except OSError:
            print(f"  Waiting... ({attempt + 1}/30)")
            time.sleep(2)
    else:
        print("ERROR: Test database not reachable on port 5433.")
        print("Run: docker compose up -d db_test")
        return 1

    # 2. Apply migrations to test DB
    print("\n" + "=" * 60)
    print("STEP 2: Applying Alembic migrations to test database...")
    env = os.environ.copy()
    env["DATABASE_URL_SYNC"] = (
        "postgresql+psycopg2://billing_test:billing_test_secret"
        "@localhost:5433/billing_engine_test"
    )
    rc = run(["python", "-m", "alembic", "upgrade", "head"], env=env)
    if rc != 0:
        print("ERROR: Migration failed.")
        return rc

    # 3. Run the test suite
    print("\n" + "=" * 60)
    print("STEP 3: Running Phase 1 database tests...")
    rc = run(
        [
            "python", "-m", "pytest",
            "tests/test_db/",
            "-v",
            "--tb=short",
            "-p", "no:asyncio",
        ],
        env={**env, "ENVIRONMENT": "testing"},
    )

    print("\n" + "=" * 60)
    if rc == 0:
        print("[PASS] Phase 1 database tests: ALL PASSED")
    else:
        print("[FAIL] Phase 1 database tests: FAILURES DETECTED")

    return rc


if __name__ == "__main__":
    sys.exit(main())
