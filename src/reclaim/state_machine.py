"""Explicit, guarded state machine for RecoveryCase lifecycle.

Rules:
1. Transitions are the ONLY way stage progress is recorded.
2. Any edge not in ``TRANSITION_TABLE`` is illegal and raises.
3. Terminal states (RESOLVED / ESCALATED / FAILED) are absorbing.
4. ``DECIDED -> RESOLVED`` is legal ONLY when the decision was ``stop``
   (guarded via ``via_stop``), so "resolved by a deliberate halt" is
   distinguishable from "resolved by a successful recovery".

Every accepted transition logs: current state, the trigger, and the next
state — the structured metadata block that feeds the audit trail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .models import Action, CaseState

logger = logging.getLogger("reclaim.state_machine")

# Valid transitions. ``None`` is the pre-creation state (case row insert).
# ``DECIDED -> RESOLVED`` sits in the table but is additionally gated by
# ``via_stop`` in ``validate_transition``.
TRANSITION_TABLE: dict[CaseState | None, set[CaseState]] = {
    None: {CaseState.INGESTED},
    CaseState.INGESTED: {CaseState.DIAGNOSED},
    CaseState.DIAGNOSED: {CaseState.DECIDED},
    CaseState.DECIDED: {CaseState.ACTING, CaseState.RESOLVED},
    CaseState.ACTING: {CaseState.RESOLVED, CaseState.ESCALATED, CaseState.FAILED},
}

class IllegalTransitionError(Exception):
    """Raised when a stage attempts a transition the table forbids."""


# `prev` is None for the very first transition (case row creation).
TransitionListener = Callable[[CaseState | None, str, CaseState], None]


def validate_transition(
    current: CaseState | None,
    target: CaseState,
    *,
    via_stop: bool = False,
    action: Action | None = None,
) -> None:
    """Validate (and for DECIDED->RESOLVED scope) an edge without mutating."""

    allowed = TRANSITION_TABLE.get(current, set())
    if target not in allowed:
        raise IllegalTransitionError(
            f"Illegal transition {current} -> {target} (not in transition table)"
        )
    if current == CaseState.DECIDED and target == CaseState.RESOLVED and not via_stop:
        proposed = f" (action={action.value if action else None})"
        raise IllegalTransitionError(
            "DECIDED -> RESOLVED is only legal when the decision was 'stop'; "
            f"got via_stop=False{proposed}"
        )


def is_terminal(state: CaseState) -> bool:
    return state.is_terminal()


class CaseStateMachine:
    """Carries a case through its lifecycle, firing listeners on each move.

    ``on_transition`` is an injection point for the audit-log writer; stages
    pass it in so the trail is written at the exact moment state changes.
    """

    def __init__(
        self,
        initial: CaseState | None = None,
        on_transition: TransitionListener | None = None,
    ) -> None:
        self.current: CaseState | None = initial
        self._on_transition = on_transition

    # -- lifecycle helpers -------------------------------------------------

    def ingest(self) -> CaseState:
        return self._move(CaseState.INGESTED, trigger="webhook.verified.non_duplicate")

    def diagnose(self) -> CaseState:
        return self._move(CaseState.DIAGNOSED, trigger="diagnose.completed")

    def decide(self) -> CaseState:
        return self._move(CaseState.DECIDED, trigger="decide.completed")

    def start_acting(self) -> CaseState:
        return self._move(CaseState.ACTING, trigger="act.started")

    def resolve_as_stopped(self) -> CaseState:
        """DECIDED -> RESOLVED via a deliberate ``stop`` decision."""
        if self.current != CaseState.DECIDED:
            raise IllegalTransitionError(
                f"resolve_as_stopped requires DECIDED, got {self.current}"
            )
        return self._move(CaseState.RESOLVED, trigger="decision.stop", via_stop=True)

    def resolve(self) -> CaseState:
        return self._move(CaseState.RESOLVED, trigger="act.succeeded")

    def escalate(self) -> CaseState:
        return self._move(CaseState.ESCALATED, trigger="act.escalated")

    def fail(self) -> CaseState:
        return self._move(CaseState.FAILED, trigger="act.failed")

    # -- core ---------------------------------------------------------------

    def _move(
        self,
        target: CaseState,
        *,
        trigger: str,
        via_stop: bool = False,
        action: Action | None = None,
    ) -> CaseState:
        validate_transition(self.current, target, via_stop=via_stop, action=action)
        if self.current is not None and is_terminal(self.current):
            raise IllegalTransitionError(
                f"{self.current} is terminal and absorbing; cannot move to {target}"
            )
        state_from, state_to = self.current, target
        self.current = target
        if self._on_transition is not None:
            self._on_transition(state_from, trigger, state_to)
        logger.info(
            "STATE_TRANSITION current=%s trigger=%s next=%s",
            state_from.value if state_from else None,
            trigger,
            state_to.value,
        )
        return target


def run_decision_flow(
    machine: CaseStateMachine, decision: Action
) -> CaseState:
    """Route a decided action through the state machine.

    ``stop`` resolves immediately (no external side effect); every other
    action must pass through ACTING before reaching a terminal state.
    """
    if machine.current != CaseState.DECIDED:
        raise IllegalTransitionError(
            f"decision routing requires DECIDED, got {machine.current}"
        )
    if decision == Action.STOP:
        return machine.resolve_as_stopped()
    return machine.start_acting()