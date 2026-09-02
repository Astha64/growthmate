"""
Guardrail / policy layer — plain deterministic Python, no I/O, no LLM.

Revision 2 splits policy into two INDEPENDENT, sequential, deterministic
checks, each separately auditable (LLD §5, §6):

  1. `validate_approval`  (LLD §6.1) — did the user explicitly approve THIS
     specific checkout? Checked by `approval_node`. Never an LLM tool.
  2. `check_transaction`   (LLD §6.2) — is the amount within spend limits?
     Checked by `guardrail_node`.

Rule order in check_transaction (first failing rule wins):
  1. actor not in ALLOWED_ACTORS        -> block "unknown actor"
  2. amount > MAX_PER_TRANSACTION       -> block per-transaction
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


# ---------------------------------------------------------------------------
# §6.1  Approval validation — fail-closed, never an LLM tool.
# ---------------------------------------------------------------------------

# A small fixed set of affirmative patterns, tied to the specific confirmation
# question `approval_node` expects the agent to have asked (e.g. "shall I
# proceed with this checkout?"). Anything not in this set — or any missing
# precondition — is treated as NOT approved (fail-closed).
AFFIRMATIVE_PATTERNS = {
    "yes",
    "yep",
    "yeah",
    "ya",
    "go ahead",
    "proceed",
    "confirm",
    "confirmed",
    "approved",
    "looks good",
    "sure",
    "ok",
    "okay",
    "yes please",
    "do it",
    "that works",
    "sounds good",
}

# Negation words that flip any affirmative match back to NOT approved
# (fail-closed): "no", "not sure", "don't", etc.
NEGATION_WORDS = (
    "no", "nope", "not", "never", "don't", "dont", "do not",
    "not now", "wait", "hold on", "maybe", "perhaps",
)


def _is_affirmative(text: str) -> bool:
    """Return True if `text` is an explicit approval (containing an affirmative
    phrase and no negation), False otherwise."""
    # Any negation word anywhere flips a would-be match back to False
    # (fail-closed). "not sure" must NOT count as approval even though it
    # contains "sure". Word-boundary check avoids matching inside "donut".
    negated = any(
        f" {w} " in f" {text} " or text.startswith(f"{w} ") or text == w or text.endswith(f" {w}")
        for w in NEGATION_WORDS
    )
    if negated:
        return False
    hit = any(p in text for p in AFFIRMATIVE_PATTERNS if p)
    return hit


def validate_approval(state: dict) -> bool:
    """
    True only if:
      - state['checkout_preview'] is set (a checkout preview was actually
        shown to this user in this same conversation), AND
      - the most recent user message, checked against AFFIRMATIVE_PATTERNS, is
        classified as explicit approval.

    Fail-closed: any ambiguity, missing precondition, or classification error
    returns False, never True. Approval is a state transition tied to a prior
    shown checkout preview — not a standalone sentiment judgment.
    """
    preview = state.get("checkout_preview")
    if not preview:
        return False

    messages = state.get("messages") or []
    last_user = None
    for msg in reversed(messages):
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        else:
            role = getattr(msg, "type", None)
            content = getattr(msg, "content", None)
        if role == "user" and content is not None:
            last_user = str(content).strip().lower()
            break

    if not last_user:
        return False

    return _is_affirmative(last_user)


# ---------------------------------------------------------------------------
# §6.2  Transaction spend-limit check — checked against cart_total.
# ---------------------------------------------------------------------------

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
