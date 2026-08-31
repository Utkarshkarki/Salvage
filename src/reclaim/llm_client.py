"""LLM client: OpenAI-compatible client for local Ollama + deterministic offline shim.

Both paths share the same retry-once-with-validation-error wrapper:

  call -> parse+validate -> on ANY failure:
    retry ONCE with the validation error appended to the prompt
    if still failing: deterministic fallback, fallback_triggered=True
  Returns (validated_output, fallback_triggered: bool)

This is the ONLY way LLM failures are handled — no silent guesses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Generic, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .config import Settings, get_settings
from .models import (
    Action,
    Cause,
    DecideInput,
    DecideOutput,
    DiagnoseInput,
    DiagnoseOutput,
)

logger = logging.getLogger("reclaim.llm_client")

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Provenance + adversarial-input triage
# ---------------------------------------------------------------------------
# The ``decline_code`` field is free text that flows into the Diagnose prompt.
# A hostile/malformed value must NEVER steer the diagnosis, so we triage it
# BEFORE the LLM is consulted and short-circuit to a deterministic fallback
# when it looks adversarial. ``prompt_version`` + ``prompt_hash`` give each
# Diagnose/Decide call a reproducible identity for drift detection/audits.

# Markers that strongly suggest prompt-injection / instruction overrides.
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all",
    "ignore the above",
    "system prompt",
    "system:",
    "you are now",
    "your instructions",
    "role: system",
    "role:user",
    "<|im_start",
    "<|im_end",
    "### instruction",
    "### human",
    "forget everything",
    "act as",
    "do not follow",
    "disregard",
)

# A real bank decline code is short (R01, 54, Z06...). Anything pathological is
# treated as untrusted noise rather than rerouted into the model.
_MAX_DECLINE_CODE_LEN = 40

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_STRUCT_RE = re.compile(r"[{}\[\]\":\\]")


class TriageResult:
    """Outcome of triaging a Diagnose input for adversarial content."""

    def __init__(self, triaged: bool, reason: str = "") -> None:
        self.triaged = triaged
        self.reason = reason


def triage_diagnose_input(input_: DiagnoseInput) -> TriageResult:
    """Detect malformed / injection-attempting input on the unknown-text path.

    Returns ``triaged=True`` with a reason when the decline code must not reach
    the model. Plain bank codes (and empty-free-text edge cases) pass through.
    """
    code = input_.decline_code or ""
    low = code.lower()

    if _CONTROL_CHAR_RE.search(code):
        return TriageResult(True, "decline_code contains control characters")
    # Injection markers rank above "too long" — a hostile string that is ALSO
    # long is still first and foremost an injection attempt.
    if any(m in low for m in _INJECTION_MARKERS):
        return TriageResult(True, "decline_code contains prompt-injection markers")
    if _JSON_STRUCT_RE.search(code):
        # A structured/templated string masquerading as a decline code.
        return TriageResult(True, "decline_code contains structured/JSON characters")
    if len(code) > _MAX_DECLINE_CODE_LEN:
        return TriageResult(
            True, f"decline_code too long ({len(code)} > {_MAX_DECLINE_CODE_LEN} chars)"
        )
    return TriageResult(False)


def _provenance(settings: Settings, kind: str, content_hash_source: str) -> dict[str, str]:
    """Deterministic provenance identity for one Diagnose/Decide call.

    ``content_hash_source`` is the serialized prompt/input the call consumes.
    """
    digest = hashlib.sha256(
        (f"{settings.ollama_model}|{kind}|{content_hash_source}").encode("utf-8")
    ).hexdigest()
    return {
        "model": settings.ollama_model,
        "prompt_version": f"{kind}-v1",
        "prompt_hash": digest,
        "mode": settings.llm_mode,
    }

# ---------------------------------------------------------------------------
# Offline deterministic shim (hermetic, no network, stable for tests/demo)
# ---------------------------------------------------------------------------


def offline_diagnose(input_: DiagnoseInput) -> DiagnoseOutput:
    """Deterministic rule table keyed on the raw decline code.

    The map covers all known codes used by the synthetic generator. Anything
    not in the table -> UNKNOWN with low confidence.
    """
    code = input_.decline_code.upper()
    if code in ("R01", "R02"):
        cause = Cause.INSUFFICIENT_FUNDS
        conf = 0.95
    elif code in ("54", "F14"):
        cause = Cause.CARD_EXPIRED
        conf = 0.97
    elif code in ("91", "Z06"):
        cause = Cause.BANK_TIMEOUT
        conf = 0.9
    elif code in ("05", "N7"):
        cause = Cause.DO_NOT_HONOR
        conf = 0.92
    elif code in ("R0", "PM"):
        cause = Cause.MANDATE_REVOKED
        conf = 0.98
    elif code in ("255", "C6"):
        cause = Cause.UNKNOWN
        conf = 0.5
    else:
        cause = Cause.UNKNOWN
        conf = 0.2

    history_len = len(input_.payment_history)
    reasoning = f"decline_code={code} -> {cause.value}; history={history_len} records"
    return DiagnoseOutput(cause=cause, confidence=conf, reasoning=reasoning)


def offline_decide(input_: DecideInput) -> DecideOutput:
    """Offline Decide shim: a deliberately NAIVE proposer.

    It almost always wants to ``retry_now`` — even when that is unsafe (a
    revoked mandate, exhausted attempts, a huge amount, within cooldown). The
    stopping-rule layer in :mod:`reclaim.stopping_rules` then clamps each
    unsafe proposal back down in code. That is exactly the "LLM proposes, code
    disposes" dynamic the build wants visible.

    Occasionally (deterministic on the inputs) it proposes a scheduled retry
    or a payment-method-update so the batch exercises those bounded actions.
    """

    now = datetime.now(UTC)
    reasoning = (
        f"cause={input_.cause.value} attempt={input_.attempt_number} "
        f"days_since={input_.days_since_last_attempt} amount={input_.amount}"
    )
    action: Action = Action.RETRY_NOW
    scheduled_at: datetime | None = None

    # Deterministic pseudo-variety from the inputs (no RNG state).
    seed_str = f"{input_.attempt_number}{input_.days_since_last_attempt}{input_.amount}"
    h = (sum(ord(c) * (i + 3) for i, c in enumerate(seed_str)) % 10)
    if h == 1:
        action = Action.RETRY_SCHEDULED
        scheduled_at = now + timedelta(hours=48)
    elif h == 2:
        action = Action.REQUEST_PAYMENT_METHOD_UPDATE

    return DecideOutput(action=action, scheduled_at=scheduled_at, reasoning=reasoning)


# ---------------------------------------------------------------------------
# Online client (OpenAI SDK -> Ollama /v1)
# ---------------------------------------------------------------------------


class LLMClient:
    """OpenAI-compatible client pointed at a local Ollama endpoint.

    Uses JSON mode (response_format) to force schema-compliant output.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                base_url=f"{self.settings.ollama_base_url}/v1",
                api_key="ollama",  # Ollama ignores this; required by SDK
                timeout=self.settings.ollama_timeout_seconds,
            )
        return self._client

    def diagnose(self, input_: DiagnoseInput) -> DiagnoseOutput:
        """Call the LLM for a root-cause diagnosis.

        In ``offline`` mode, delegates to the deterministic shim.
        In ``online`` mode, calls the model with a structured prompt.
        """
        if self.settings.llm_mode == "offline":
            return offline_diagnose(input_)

        system = (
            "You are a precise payment failure classifier. "
            "Output ONLY a JSON object matching the DiagnoseOutput schema."
        )
        user = (
            "Analyze the payment failure and return: cause (one of "
            "insufficient_funds, card_expired, bank_timeout, do_not_honor, "
            "mandate_revoked, unknown), confidence (0..1), and a short "
            "reasoning string. Use the decline code and recent payment history.\n\n"
            f"Decline code: {input_.decline_code}\n"
            f"Payment history: {len(input_.payment_history)} records"
        )
        return self._call_with_json_mode(DiagnoseOutput, system, user)

    def decide(self, input_: DecideInput) -> DecideOutput:
        """Call the LLM for a bounded action proposal.

        In ``offline`` mode delegates to the naive shim. In ``online`` mode
        calls the model with JSON-mode structured output. NOTE: the output is
        a *proposal* — the stopping-rule layer must still run over it.
        """
        if self.settings.llm_mode == "offline":
            return offline_decide(input_)

        system = (
            "You are a cautious payment-recovery action planner. "
            "Output ONLY a JSON object matching the DecideOutput schema."
        )
        user = (
            "Choose exactly ONE action from: retry_now, retry_scheduled, "
            "request_payment_method_update, escalate_human, stop. If "
            "retry_scheduled, include a scheduled_at ISO-8601 datetime in the "
            "future; it must be absent for all other actions. Include a short "
            "reasoning string.\n\n"
            f"cause={input_.cause.value}\n"
            f"attempt_number={input_.attempt_number}\n"
            f"days_since_last_attempt={input_.days_since_last_attempt}\n"
            f"amount={input_.amount}\n"
            f"customer_tier={input_.customer_tier}"
        )
        return self._call_with_json_mode(DecideOutput, system, user)

    # -----------------------------------------------------------------------
    # Internal: JSON-mode call with retry-once-on-validation
    # -----------------------------------------------------------------------

    def _call_with_json_mode(self, model: type[T], system: str, user: str) -> T:
        """Single LLM call with one automatic retry on validation failure.

        On first failure, the validation error message is appended to the user
        prompt and we retry ONCE. If the second attempt also fails, the
        deterministic fallback for that model is returned with
        fallback_triggered=True (handled by the caller wrapper).
        """
        client = self._get_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=self.settings.ollama_model,
                    messages=messages,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    timeout=self.settings.ollama_timeout_seconds,
                )
                content = resp.choices[0].message.content
                if content is None:
                    raise ValueError("empty LLM response")
                parsed = model.model_validate_json(content)
                return parsed
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "LLM call attempt %d failed: %s", attempt, exc
                )
                if attempt == 1:
                    # Retry once with the validation error fed back.
                    err_msg = f"VALIDATION ERROR: {exc}"
                    messages.append({"role": "assistant", "content": content or "{}", })
                    messages.append({"role": "user", "content": err_msg})
                    continue
                # Second failure -> let caller handle fallback.
                raise

        # Unreachable; the loop raises on attempt 2 failure.
        raise RuntimeError("LLM call loop exhausted")


# ---------------------------------------------------------------------------
# Shared wrapper: call -> validate -> retry-once -> fallback
# ---------------------------------------------------------------------------


class FallbackResult(Generic[T]):
    output: T
    fallback_triggered: bool
    provenance: dict[str, str] | None

    def __init__(
        self,
        output: T,
        fallback_triggered: bool,
        provenance: dict[str, str] | None = None,
    ) -> None:
        self.output = output
        self.fallback_triggered = fallback_triggered
        self.provenance = provenance


class LLMWrapper:
    """Convenience wrapper exposing diagnose/decide with fallback + triage built in."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = LLMClient(settings)

    def diagnose(self, input_: DiagnoseInput) -> FallbackResult[DiagnoseOutput]:
        # Adversarial-input guard: malformed / injection-attempting decline
        # codes are triaged BEFORE the LLM is consulted. The model never sees
        # the hostile string, so its "response" (had one happened) can never
        # influence the result — the deterministic fallback governs.
        triage = triage_diagnose_input(input_)
        source = input_.model_dump_json()
        prov = _provenance(self.settings, "diagnose", source)
        if triage.triaged:
            logger.warning("DIAGNOSE_TRIAGED reason=%s", triage.reason)
            fallback = DiagnoseOutput(
                cause=Cause.UNKNOWN,
                confidence=0.0,
                reasoning=f"adversarial/malformed decline_code triaged: "
                          f"{triage.reason}; LLM not consulted",
            )
            return FallbackResult(
                fallback, fallback_triggered=False, provenance=prov
            )
        try:
            return FallbackResult(
                self.client.diagnose(input_), fallback_triggered=False, provenance=prov
            )
        except Exception as exc:  # timeout, validation after retry, etc.
            logger.error("Diagnose fallback triggered: %s", exc)
            fallback = DiagnoseOutput(
                cause=Cause.UNKNOWN,
                confidence=0.0,
                reasoning=f"fallback: {type(exc).__name__}",
            )
            return FallbackResult(fallback, fallback_triggered=True, provenance=prov)

    def decide(self, input_: DecideInput) -> FallbackResult[DecideOutput]:
        """Decide with the deterministic fallback: escalate_human.

        Per the spec, if the LLM still fails after the retry-once-with-error
        path, Reclaim deterministically falls back to escalate_human rather
        than guessing at any money-moving action."""
        prov = _provenance(self.settings, "decide", input_.model_dump_json())
        try:
            return FallbackResult(
                self.client.decide(input_), fallback_triggered=False, provenance=prov
            )
        except Exception as exc:  # timeout, validation after retry, etc.
            logger.error("Decide fallback triggered: %s", exc)
            fallback = DecideOutput(
                action=Action.ESCALATE_HUMAN,
                reasoning=f"fallback: {type(exc).__name__} - escalate human",
            )
            return FallbackResult(fallback, fallback_triggered=True, provenance=prov)