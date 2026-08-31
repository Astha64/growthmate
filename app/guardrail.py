"""
Guardrail / policy layer — plain deterministic Python, no I/O, no LLM.

Matches LOW_LEVEL_DESIGN.md §5 exactly:
  - MAX_PER_TRANSACTION / MAX_PER_SESSION per actor
  - ALLOWED_ACTORS allow-list
  - check_transaction returns a GuardrailDecision with rule-ordered reasons.

Rule order (first failing rule wins):
  1. actor not in ALLOWED_ACTORS  -> block "unknown actor"
  2. amount > MAX_PER_TRANSACTION -> block per-transaction
  3. spend_so_far + amount > MAX_PER_SESSION -> block per-session
  4. else -> allow

This module does NOT import from tools.py or main.py, and performs no DB or
network access — fully unit-testable in isolation (tests/test_guardrail.py).
"""

from dataclasses import dataclass

MAX_PER_TRANSACTION = {"human": 5000.0, "buyer_agent": 3000.0}
MAX_PER_SESSION = {"human": 15000.0, "buyer_agent": 5000.0}
ALLOWED_ACTORS = {"human", "buyer_agent"}


@dataclass
class GuardrailDecision:
    allowed: bool
    reason: str


def check_transaction(actor: str, amount: float, spend_so_far: float) -> GuardrailDecision:
    """Pure function: same inputs always produce the same output."""
    if actor not in ALLOWED_ACTORS:
        return GuardrailDecision(allowed=False, reason="unknown actor")

    if amount > MAX_PER_TRANSACTION[actor]:
        return GuardrailDecision(
            allowed=False,
            reason=f"exceeds per-transaction limit of ₹{int(MAX_PER_TRANSACTION[actor])}",
        )

    if spend_so_far + amount > MAX_PER_SESSION[actor]:
        return GuardrailDecision(
            allowed=False,
            reason=f"exceeds per-session limit of ₹{int(MAX_PER_SESSION[actor])}",
        )

    return GuardrailDecision(allowed=True, reason="within limits")
