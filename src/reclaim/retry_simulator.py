"""Shared retry-success simulation model.

This is the single source of truth for "does a retry on this decline code
actually succeed?" used by both the real pipeline (stub mode) and the baseline
comparison (counterfactual). Both must face the same underlying success
probabilities — they differ only in WHICH cases they decide to retry
(stopping rules) — not in the odds of success once attempted.

The rates are informed by industry data on retry success by decline category.
"""

import random

# Realistic retry success rates by decline code (raw bank codes from Razorpay).
# These model the actual probability that a retry will succeed, given the
# underlying reason for the failure.
RETRY_SUCCESS_RATE: dict[str, float] = {
    "R01": 0.45,  # Insufficient funds: ~45% succeed (customer adds money)
    "R02": 0.45,  # Insufficient funds (variant)
    "54": 0.05,   # Card expired: ~5% succeed (card rarely re-activated by retry)
    "F14": 0.05,  # Card expired (variant)
    "91": 0.60,   # Bank timeout: ~60% succeed (often temporary)
    "Z06": 0.60,  # Bank timeout (variant)
    "05": 0.35,   # Do not honor: ~35% succeed (issuer discretion)
    "N7": 0.35,   # Do not honor (variant)
    "R0": 0.00,   # Mandate revoked: 0% succeed (can't retry without new mandate)
    "PM": 0.00,   # Mandate revoked (variant)
    "255": 0.25,  # Unknown: 25% base rate (uncertain, conservative)
    "C6": 0.25,   # Unknown (variant)
}


def retry_would_succeed(failure_reason: str, rng: random.Random | None = None) -> bool:
    """Simulate whether a retry would succeed for a given decline code.

    Args:
        failure_reason: The raw bank decline code (e.g. "R01", "54", "91").
        rng: Optional seeded random.Random for deterministic simulation.
             If None, uses the global random.

    Returns:
        True if the retry succeeds (gateway accepts it), False otherwise.
    """
    rate = RETRY_SUCCESS_RATE.get(failure_reason, 0.25)  # default for unknown codes
    if rng is None:
        rng = random
    return rng.random() < rate
