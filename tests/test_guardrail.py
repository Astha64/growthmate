"""
Pure-function tests for the guardrail layer. Must NOT touch the DB or network
(AGENTS.md gotcha). Table-driven, no mocking (LLD §11.8).
"""

from app.guardrail import ALLOWED_ACTORS, MAX_PER_SESSION, MAX_PER_TRANSACTION, check_transaction

HUMAN_TX = MAX_PER_TRANSACTION["human"]  # 5000
BA_TX = MAX_PER_TRANSACTION["buyer_agent"]  # 3000
HUMAN_SESSION = MAX_PER_SESSION["human"]  # 15000
BA_SESSION = MAX_PER_SESSION["buyer_agent"]  # 5000


def test_unknown_actor_blocked():
    d = check_transaction("evil", 10.0, 0.0)
    assert d.allowed is False
    assert d.reason == "unknown actor"


def test_all_actors_allowed():
    assert ALLOWED_ACTORS == {"human", "buyer_agent"}


def test_within_limits_human():
    d = check_transaction("human", 1500.0, 0.0)
    assert d.allowed is True
    assert d.reason == "within limits"


def test_within_limits_buyer_agent():
    d = check_transaction("buyer_agent", 2999.0, 0.0)
    assert d.allowed is True


def test_exact_transaction_limit_allowed():
    d = check_transaction("human", HUMAN_TX, 0.0)
    assert d.allowed is True


def test_over_transaction_limit_human():
    d = check_transaction("human", HUMAN_TX + 1.0, 0.0)
    assert d.allowed is False
    assert f"per-transaction limit of ₹{int(HUMAN_TX)}" in d.reason


def test_over_transaction_limit_buyer_agent():
    # Engineered failure (§11.5): APP-001 x10 = 4990 > 3000
    d = check_transaction("buyer_agent", 4990.0, 0.0)
    assert d.allowed is False
    assert "per-transaction limit of ₹3000" in d.reason


def test_at_transaction_limit_buyer_agent():
    d = check_transaction("buyer_agent", BA_TX, 0.0)
    assert d.allowed is True


def test_over_session_limit_human():
    d = check_transaction("human", 1000.0, HUMAN_SESSION - 500.0)
    assert d.allowed is False
    assert f"per-session limit of ₹{int(HUMAN_SESSION)}" in d.reason


def test_over_session_limit_buyer_agent():
    d = check_transaction("buyer_agent", 1000.0, BA_SESSION - 500.0)
    assert d.allowed is False


def test_exact_session_limit_allowed():
    # spend_so_far such that spend + amount == limit exactly -> allowed (not >)
    d = check_transaction("human", 1000.0, HUMAN_SESSION - 1000.0)
    assert d.allowed is True


def test_negative_or_zero_amount_within_limits():
    d = check_transaction("human", 0.0, 0.0)
    assert d.allowed is True


def test_pure_function_deterministic():
    # Same inputs -> same output, always (stateless)
    a = check_transaction("buyer_agent", 2500.0, 0.0)
    b = check_transaction("buyer_agent", 2500.0, 0.0)
    assert a.allowed == b.allowed and a.reason == b.reason
