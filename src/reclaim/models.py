"""Pydantic v2 schemas for every agent input/output and API payload.

Every boundary is validated with Pydantic — no implicit fields, no silently
dropped validation (ZERO-HALO mandate).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CaseState(StrEnum):
    """Lifecycle states for a RecoveryCase. Transitions are the ONLY way
    stage progress is recorded — see ``state_machine.py``."""

    INGESTED = "INGESTED"
    DIAGNOSED = "DIAGNOSED"
    DECIDED = "DECIDED"
    ACTING = "ACTING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"

    def is_terminal(self) -> bool:
        return self in (CaseState.RESOLVED, CaseState.ESCALATED, CaseState.FAILED)


class Cause(StrEnum):
    """Root-cause taxonomy produced by the Diagnose agent."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    BANK_TIMEOUT = "bank_timeout"
    DO_NOT_HONOR = "do_not_honor"
    MANDATE_REVOKED = "mandate_revoked"
    UNKNOWN = "unknown"


class Action(StrEnum):
    """Exactly one bounded action may come out of the Decide agent."""

    RETRY_NOW = "retry_now"
    RETRY_SCHEDULED = "retry_scheduled"
    REQUEST_PAYMENT_METHOD_UPDATE = "request_payment_method_update"
    ESCALATE_HUMAN = "escalate_human"
    STOP = "stop"


class WebhookType(StrEnum):
    """Razorpay webhook event types Reclaim subscribes to."""

    PAYMENT_FAILED = "payment.failed"
    SUBS_CHARGE_FAILED = "subscription.charged.failed"
    SUBS_PENDING = "subscription.pending"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class PaymentRecord(BaseModel):
    """A single line of a customer's payment history for a subscription."""

    status: str = Field(min_length=1)
    amount: float = Field(gt=0)
    attempted_at: datetime


class RecoveryCase(BaseModel):
    """The persisted state of one recovery workflow.

    ``event_id`` is the Razorpay webhook event id and is globally unique —
    it is the dedupe key. ``case_id`` identifies the recoverable unit
    (typically the subscription id).
    """

    model_config = ConfigDict(from_attributes=True)

    case_id: str
    event_id: str
    customer_id: str
    subscription_id: str
    failure_reason: str = Field(min_length=1)  # raw bank decline code, e.g. "R01"
    amount: float = Field(gt=0, description="Amount at risk, in INR")
    attempt_number: int = Field(ge=1)
    customer_tier: str = "standard"
    payment_history: list[PaymentRecord] = Field(default_factory=list)
    state: CaseState = CaseState.INGESTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def days_since_last_attempt(self, now: datetime | None = None) -> int:
        """Whole days elapsed since the most recent failed attempt."""
        anchor = now or datetime.now(UTC)
        last = self.created_at
        if self.payment_history:
            last = max(r.attempted_at for r in self.payment_history)
        return max(0, int((anchor - last).total_seconds() // 86400))


# ---------------------------------------------------------------------------
# Agent input / output schemas
# ---------------------------------------------------------------------------


class DiagnoseInput(BaseModel):
    """What the Diagnose agent reasons over."""

    decline_code: str = Field(min_length=1)
    payment_history: list[PaymentRecord] = Field(default_factory=list)


class DiagnoseOutput(BaseModel):
    """Pydantic-validated output of the Diagnose agent."""

    cause: Cause
    confidence: float  # 0..1, validated by ``_clamp_confidence``
    reasoning: str = Field(min_length=1)

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence out of bounds [0,1]: {v}")
        return round(float(v), 4)


class DecideInput(BaseModel):
    """What the Decide agent reasons over."""

    cause: Cause
    attempt_number: int = Field(ge=1)
    days_since_last_attempt: int = Field(ge=0)
    amount: float = Field(gt=0)
    customer_tier: str = "standard"


class DecideOutput(BaseModel):
    """Pydantic-validated output of the Decide agent.

    ``scheduled_at`` is REQUIRED for ``retry_scheduled`` and must be in the
    future; it must be absent for every other action.
    """

    action: Action
    scheduled_at: datetime | None = None
    reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_cross_field_rule(self) -> DecideOutput:
        # NOTE: enforced with a model_validator (not a field_validator) because
        # Pydantic skips field validators when a field uses its default value,
        # and `scheduled_at=None` is exactly that case.
        if self.action == Action.RETRY_SCHEDULED and self.scheduled_at is None:
            raise ValueError("retry_scheduled requires a scheduled_at datetime")
        if self.action != Action.RETRY_SCHEDULED and self.scheduled_at is not None:
            raise ValueError("scheduled_at is only allowed for retry_scheduled")
        if self.scheduled_at is not None:
            at = self.scheduled_at
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at <= datetime.now(UTC):
                raise ValueError("scheduled_at must be in the future for retry_scheduled")
        return self


# ---------------------------------------------------------------------------
# Audit + webhook
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """One immutable row in the append-only audit trail.

    ``fallback_triggered`` records whether this stage hit a deterministic
    fallback instead of a clean LLM output — the demo highlights this.
    """

    model_config = ConfigDict(from_attributes=True)

    case_id: str
    stage: str  # e.g. "ingest", "diagnose", "decide", "act"
    agent_reasoning: str = ""
    input_state: dict[str, Any] = Field(default_factory=dict)
    decision: str = ""
    action_taken: str | None = None
    outcome: str = ""
    fallback_triggered: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WebhookEvent(BaseModel):
    """Parsed Razorpay webhook payload (before signature verification)."""

    event_id: str = Field(min_length=1)
    type: WebhookType
    payload: dict[str, Any] = Field(default_factory=dict)

    def subscription_id(self) -> str:
        """Best-effort id of the recoverable unit, never fabricated."""
        entity = self.payload.get("entity", {})
        sub = entity.get("subscription_id")
        pay = entity.get("id")
        return str(sub or pay or self.event_id)

    def case_id(self) -> str:
        return self.subscription_id()

    def customer_id(self) -> str:
        entity = self.payload.get("entity", {})
        return str(entity.get("customer_id") or "unknown")

    def amount(self) -> float:
        entity = self.payload.get("entity", {})
        try:
            val = float(entity.get("amount", 0.0))
        except (TypeError, ValueError):
            val = 0.0
        # Razorpay reports amounts in paise (smallest currency unit).
        return val / 100.0

    def failure_reason(self) -> str:
        entity = self.payload.get("entity", {})
        return str(entity.get("error_code") or entity.get("reason") or "unknown")


def new_event_id() -> str:
    """Deterministic-ish unique event id generator for synthetic/webhook use."""
    return f"evt_{uuid.uuid4().hex}"