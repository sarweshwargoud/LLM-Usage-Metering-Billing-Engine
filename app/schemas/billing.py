"""
Pydantic schemas for billing and Stripe Checkout endpoints.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    """
    Checkout request payload.
    Security: Accepts only valid internal paid plan slugs.
    Arbitrary Stripe Price IDs or Customer IDs from clients are forbidden.
    """
    plan: Literal["pro"] = Field(
        default="pro",
        description="Internal plan slug to purchase (e.g. 'pro')",
    )


class CheckoutResponse(BaseModel):
    checkout_url: str = Field(description="Hosted Stripe Checkout URL for customer redirect")
    session_id: str = Field(description="Stripe Checkout Session ID (cs_test_...)")
    plan: str = Field(description="Subscribed plan slug")
    stripe_customer_id: str = Field(description="Associated Stripe Customer ID")
