"""State-machine legality: valid lifecycle, illegal edges, terminal absorption,
and the DECIDED->RESOLVED stop guard."""

from __future__ import annotations

import pytest

from reclaim.models import Action, CaseState
from reclaim.state_machine import (
    CaseStateMachine,
    IllegalTransitionError,
    is_terminal,
    run_decision_flow,
    validate_transition,
)


def test_full_lifecycle_is_legal() -> None:
    machine = CaseStateMachine()
    assert machine.ingest() == CaseState.INGESTED
    assert machine.diagnose() == CaseState.DIAGNOSED
    assert machine.decide() == CaseState.DECIDED
    assert machine.start_acting() == CaseState.ACTING
    assert machine.resolve() == CaseState.RESOLVED
    assert is_terminal(machine.current or CaseState.RESOLVED)


def test_illegal_edge_raises() -> None:
    machine = CaseStateMachine()
    machine.ingest()
    with pytest.raises(IllegalTransitionError):
        machine.decide()  # INGESTED -> DECIDED is not in the table


def test_terminal_states_are_absorbing() -> None:
    for terminal in (CaseState.RESOLVED, CaseState.ESCALATED, CaseState.FAILED):
        machine = CaseStateMachine(initial=terminal)
        with pytest.raises(IllegalTransitionError):
            machine.start_acting()


def test_decided_to_resolved_requires_stop() -> None:
    with pytest.raises(IllegalTransitionError, match="via_stop"):
        validate_transition(CaseState.DECIDED, CaseState.RESOLVED, via_stop=False)


def test_decided_to_resolved_via_stop_is_legal() -> None:
    validate_transition(CaseState.DECIDED, CaseState.RESOLVED, via_stop=True)


def test_run_decision_flow_stop_resolves_without_acting() -> None:
    machine = CaseStateMachine(initial=CaseState.DECIDED)
    assert run_decision_flow(machine, Action.STOP) == CaseState.RESOLVED
    assert machine.current == CaseState.RESOLVED


def test_run_decision_flow_retry_routes_through_acting() -> None:
    machine = CaseStateMachine(initial=CaseState.DECIDED)
    assert run_decision_flow(machine, Action.RETRY_NOW) == CaseState.ACTING
    assert machine.current == CaseState.ACTING


def test_escalate_and_fail_are_terminal() -> None:
    machine = CaseStateMachine(initial=CaseState.ACTING)
    assert machine.escalate() == CaseState.ESCALATED
    assert is_terminal(machine.current or CaseState.RESOLVED)

    machine = CaseStateMachine(initial=CaseState.ACTING)
    assert machine.fail() == CaseState.FAILED
    assert is_terminal(machine.current or CaseState.RESOLVED)


def test_listener_records_transition_metadata() -> None:
    captured: list[tuple[CaseState | None, str, CaseState]] = []

    def listener(prev: CaseState | None, trigger: str, nxt: CaseState) -> None:
        captured.append((prev, trigger, nxt))

    machine = CaseStateMachine(on_transition=listener)
    machine.ingest()
    machine.diagnose()

    assert captured[0] == (None, "webhook.verified.non_duplicate", CaseState.INGESTED)
    assert captured[1][1] == "diagnose.completed"
    assert captured[1][2] == CaseState.DIAGNOSED


def test_run_decision_flow_requires_decided() -> None:
    machine = CaseStateMachine(initial=CaseState.INGESTED)
    with pytest.raises(IllegalTransitionError):
        run_decision_flow(machine, Action.STOP)


def test_approve_retry_from_escalated() -> None:
    """An operator can deliberately re-open an ESCALATED case to ACTING."""
    m = CaseStateMachine(initial=CaseState.ESCALATED)
    assert m.approve_retry() == CaseState.ACTING


def test_resolve_human_from_escalated() -> None:
    """An operator can close an ESCALATED case to RESOLVED by hand."""
    m = CaseStateMachine(initial=CaseState.ESCALATED)
    assert m.resolve_human() == CaseState.RESOLVED


def test_manual_overrides_require_escalated() -> None:
    """Manual overrides are only legal from ESCALATED — never from a working state."""
    for start in (CaseState.INGESTED, CaseState.DIAGNOSED, CaseState.DECIDED,
                  CaseState.ACTING, CaseState.RESOLVED, CaseState.FAILED):
        m = CaseStateMachine(initial=start)
        with pytest.raises(IllegalTransitionError):
            m.approve_retry()
        with pytest.raises(IllegalTransitionError):
            m.resolve_human()


def test_manual_transition_is_audited_via_listener() -> None:
    captured: list[tuple[CaseState | None, str, CaseState]] = []

    def listener(prev, trigger, nxt):
        captured.append((prev, trigger, nxt))

    m = CaseStateMachine(initial=CaseState.ESCALATED, on_transition=listener)
    m.approve_retry()
    assert captured[-1] == (CaseState.ESCALATED, "manual.approve_retry", CaseState.ACTING)