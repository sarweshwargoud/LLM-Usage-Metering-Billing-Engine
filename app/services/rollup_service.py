"""
Monthly Usage Rollup Service.
Aggregates usage events into idempotent pre-computed monthly summaries for reporting and invoice auditing.
"""
from __future__ import annotations

from datetime import date
from sqlalchemy import text
from sqlalchemy.orm import Session


class RollupService:
    """
    Service responsible for periodic background computation of monthly usage rollups.
    """

    def compute_monthly_rollups(
        self,
        session: Session,
        billing_period_start: date | None = None,
    ) -> int:
        """
        Executes an idempotent SQL upsert aggregating usage_events for the specified billing period.

        Invariants (Scenario 20):
        - Idempotent: Can run repeatedly without creating duplicate rows or altering counts.
        - Preserves integer micro-cent monetary precision.
        """
        period = billing_period_start or date.today().replace(day=1)

        stmt = text("""
            INSERT INTO monthly_usage_rollups (
                tenant_id,
                billing_period_start,
                usage_type,
                total_quantity,
                total_cost_micro_cents,
                computed_at
            )
            SELECT
                tenant_id,
                billing_period_start,
                usage_type,
                COALESCE(SUM(quantity), 0) AS total_quantity,
                COALESCE(SUM(cost_micro_cents), 0) AS total_cost_micro_cents,
                NOW() AS computed_at
            FROM usage_events
            WHERE billing_period_start = :period
            GROUP BY tenant_id, billing_period_start, usage_type
            ON CONFLICT (tenant_id, billing_period_start, usage_type)
            DO UPDATE SET
                total_quantity         = EXCLUDED.total_quantity,
                total_cost_micro_cents = EXCLUDED.total_cost_micro_cents,
                computed_at            = EXCLUDED.computed_at;
        """)

        result = session.execute(stmt, {"period": period})
        return result.rowcount or 0
