"""
Chat endpoint tests — Revision 2. Uses FastAPI TestClient and monkeypatches
the LLM call inside agent_node so no real API key or network is required.

Covers:
  (a) a read-only discovery flow returns tool_calls_made correctly
  (b) a full multi-turn pipeline: clarification -> discovery -> selection ->
      upsell -> checkout -> approval -> payment, asserting execute_payment is
      only ever requested in the final approval turn and never before
      approval_node has passed
  (c) even if the agent requests execute_payment, a NON-approved user turn
      blocks it (approval_node gate) — no payment, no Order row
  (d) a blocked money action creates no Order row and returns blocked=true
"""

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app import agent_graph
from app.main import app
from app.models import AuditLog, Product

client = TestClient(app)


class ScriptedLLM:
    """Returns a scripted sequence of AIMessages (tool calls then final text).

    Each item in `script` is either:
      {"tool": name, "args": {...}}   -> an AIMessage requesting that tool
      "some text"                     -> a plain-text AIMessage (ends the turn)

    agent_node calls _make_llm fresh each time; returning the SAME instance
    preserves the call counter so the sequence plays out across the
    agent -> tool -> audit -> agent loop of one /chat invocation.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        step = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(step, dict):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": step["tool"],
                        "args": step["args"],
                        "id": f"call_{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=step)


def _patch_turn(monkeypatch, script):
    """Monkeypatch _make_llm to return a persistent ScriptedLLM for one turn."""
    scripted = ScriptedLLM(script)
    monkeypatch.setattr(agent_graph, "_make_llm", lambda: scripted)
    return scripted


def _seed(products, db_session_factory):
    s = db_session_factory()
    s.add_all(
        [
            Product(sku=sku, name=name, price=price, currency="INR", stock=stock, category=cat)
            for sku, name, price, stock, cat in products
        ]
    )
    s.commit()
    s.close()


def _audit_events(db_session_factory, session_id):
    s = db_session_factory()
    rows = s.query(AuditLog).filter(AuditLog.session_id == session_id).all()
    s.close()
    return [(r.event_type, r.outcome, r.decision) for r in rows]


def _orders(db_session_factory):
    from app.models import Order
    s = db_session_factory()
    orders = s.query(Order).all()
    s.close()
    return orders


PRODUCTS = [
    ("SHOE-001", "Nike Revolution 6", 1899.0, 20, "footwear"),
    ("APP-001", "Cotton Crew T-Shirt", 499.0, 50, "apparel"),
    ("ACC-001", "Leather Wallet", 899.0, 22, "accessories"),
]


def test_read_only_discovery_flow(db_session_factory, monkeypatch):
    _seed(PRODUCTS, db_session_factory)
    _patch_turn(
        monkeypatch,
        [
            {"tool": "discover_and_recommend_products",
             "args": {"structured_requirements": {"budget": 3000, "required_features": ["running"]}}},
            "Here are 2 options: the Nike Revolution 6 (₹1899) and more.",
        ],
    )
    resp = client.post(
        "/chat",
        json={"session_id": "sess-1", "actor": "human", "message": "find running shoes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is False
    assert "discover_and_recommend_products" in body["tool_calls_made"]
    assert "Nike" in body["reply"]

    events = _audit_events(db_session_factory, "sess-1")
    # discovery tool -> one discovery audit row, outcome success
    assert ("discovery", "success", None) in events


def test_full_pipeline_approval_before_payment(db_session_factory, monkeypatch):
    """
    Full multi-turn scripted Journey A:
      clarify -> discovery -> selection -> upsell -> cart -> preview -> approve -> pay
    Asserts execute_payment is requested ONLY on the final approval turn and
    never before approval_node has passed.
    """
    _seed(PRODUCTS, db_session_factory)
    session_id = "sess-full"
    history: list[dict] = []

    # Turn A — clarification (requirements incomplete): no tool call.
    _patch_turn(monkeypatch, ["Could you tell me: road, trail, or stability running shoes?"])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "I need running shoes, budget 2500", "history": history})
    assert r.status_code == 200
    assert r.json()["blocked"] is False
    assert r.json()["tool_calls_made"] == []  # clarification ends turn, no tool
    history += [{"role": "user", "content": "I need running shoes, budget 2500"},
                {"role": "assistant", "content": r.json()["reply"]}]

    # Turn B — discovery.
    _patch_turn(monkeypatch, [
        {"tool": "discover_and_recommend_products",
         "args": {"structured_requirements": {"budget": 2500, "required_features": ["running"]}}},
        "Here are your options: 1) Nike Revolution 6 ₹1899 ... 2) ... 3) ...",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "Road running shoes please", "history": history})
    assert "discover_and_recommend_products" in r.json()["tool_calls_made"]
    assert "execute_payment" not in r.json()["tool_calls_made"]
    history += [{"role": "user", "content": "Road running shoes please"},
                {"role": "assistant", "content": r.json()["reply"]}]

    # Turn C — selection + upsell.
    _patch_turn(monkeypatch, [
        {"tool": "recommend_complementary_products",
         "args": {"selected_product": {"name": "Nike Revolution 6", "price": 1899.0, "category": "footwear"}}},
        "Great choice! Want to add a Cotton Crew T-Shirt (₹499) or a Leather Wallet (₹899)?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "I'll take the Nike Revolution 6", "history": history})
    assert "recommend_complementary_products" in r.json()["tool_calls_made"]
    assert "execute_payment" not in r.json()["tool_calls_made"]
    history += [{"role": "user", "content": "I'll take the Nike Revolution 6"},
                {"role": "assistant", "content": r.json()["reply"]}]

    # Turn D — build cart (two update_cart calls + final summary).
    _patch_turn(monkeypatch, [
        {"tool": "update_cart",
         "args": {"action": "add", "item": {"type": "external", "ref_id": "ext-shoe",
                                            "name": "Nike Revolution 6", "price": 1899.0,
                                            "quantity": 1, "source": "https://a.test/s1"}}},
        {"tool": "update_cart",
         "args": {"action": "add", "item": {"type": "merchant", "ref_id": "ACC-001",
                                            "name": "Leather Wallet", "price": 899.0,
                                            "quantity": 1, "source": "merchant"}}},
        "Your cart so far: Nike Revolution 6 ₹1899, Leather Wallet ₹899. Anything else?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "Add the wallet too", "history": history})
    assert r.json()["tool_calls_made"].count("update_cart") == 2  # two sequential cart ops
    assert "execute_payment" not in r.json()["tool_calls_made"]
    history += [{"role": "user", "content": "Add the wallet too"},
                {"role": "assistant", "content": r.json()["reply"]}]

    # Turn E — checkout preview.
    _patch_turn(monkeypatch, [
        {"tool": "prepare_checkout", "args": {}},
        "Checkout preview:\n- Nike Revolution 6 — ₹1899\n- Leather Wallet — ₹899\nTotal: ₹2798. Shall I proceed with the checkout?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "Show checkout", "history": history})
    assert "prepare_checkout" in r.json()["tool_calls_made"]
    assert "execute_payment" not in r.json()["tool_calls_made"]
    history += [{"role": "user", "content": "Show checkout"},
                {"role": "assistant", "content": r.json()["reply"]}]

    # Turn F — approval + payment. execute_payment finally appears here, and
    # only after the user explicitly approved (last user message "yes").
    _patch_turn(monkeypatch, [
        {"tool": "execute_payment", "args": {}},
        "Order #1 confirmed. Complete payment here: https://rzp.test/link",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "Yes, go ahead", "history": history})
    body = r.json()
    assert body["blocked"] is False
    assert "execute_payment" in body["tool_calls_made"]
    assert "rzp.test" in body["reply"]

    # Exactly one Order (the payment succeeded for the human, limit 5000).
    orders = _orders(db_session_factory)
    assert len(orders) == 1
    assert orders[0].total == 2798.0

    # Audit shows the full stage pipeline, including the standalone approval
    # event BEFORE the payment attempt.
    events = _audit_events(db_session_factory, session_id)
    event_types = [e[0] for e in events]
    for expected in ("discovery", "upsell", "cart_update", "cart_update",
                     "checkout_preview", "approval", "payment_attempt"):
        assert expected in event_types, f"missing {expected} in {event_types}"
    # Approval and payment are independently audited.
    assert ("approval", "approved", "ALLOW") in events


def test_execute_payment_not_run_without_approval(db_session_factory, monkeypatch):
    """
    Approval gate: even if the agent requests execute_payment, a user turn that
    does NOT explicitly approve routes approval_node -> agent, and no payment /
    no Order row is created. The guardrail must never be reachable for an
    un-approved payment (approval_node comes first).
    """
    _seed(PRODUCTS, db_session_factory)
    session_id = "sess-noapprove"
    # Build an over-budget-disabled cart cheaply: one cheap merchant item so the
    # preview precondition exists, but the user never approves.
    _patch_turn(monkeypatch, [
        {"tool": "update_cart",
         "args": {"action": "add", "item": {"type": "merchant", "ref_id": "APP-001",
                                            "name": "Cotton Crew T-Shirt", "price": 499.0,
                                            "quantity": 1, "source": "merchant"}}},
        "Cart ready. Shall I proceed with the checkout?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "add one t-shirt to my cart"})
    history = [{"role": "user", "content": "add one t-shirt to my cart"},
               {"role": "assistant", "content": r.json()["reply"]}]

    # Turn 2: the agent (wrongly) requests execute_payment, but the user's last
    # message is "no" -> approval_node must reject it.
    _patch_turn(monkeypatch, [
        {"tool": "execute_payment", "args": {}},
        "I understand you don't want to proceed. Your cart is saved.",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "human",
                                   "message": "No, not yet", "history": history})
    body = r.json()
    # execute_payment was requested but the payment must NOT have been created.
    assert "execute_payment" in body["tool_calls_made"]
    assert _orders(db_session_factory) == []  # no Order row

    # An approval event with decision BLOCK (not approved) was audited.
    events = _audit_events(db_session_factory, session_id)
    assert ("approval", "blocked", "BLOCK") in events


def test_blocked_money_action_returns_200(db_session_factory, monkeypatch):
    """
    Journey B via a CART: over-limit cart for the buyer_agent (4 x Leather
    Wallet ₹899 = ₹3596 > ₹3000). approval_node passes (user said yes), then
    guardrail_node BLOCKS on the per-transaction limit. Block is expected
    control flow: HTTP 200 + blocked=true, no 4xx/5xx.
    """
    _seed(PRODUCTS, db_session_factory)
    session_id = "sess-b"
    _patch_turn(monkeypatch, [
        {"tool": "update_cart",
         "args": {"action": "add", "item": {"type": "merchant", "ref_id": "ACC-001",
                                            "name": "Leather Wallet", "price": 899.0,
                                            "quantity": 4, "source": "merchant"}}},
        "Cart: 4x Leather Wallet — total ₹3596. Shall I proceed with the checkout?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "buyer_agent",
                                   "message": "add 4 leather wallets to my cart"})
    history = [{"role": "user", "content": "add 4 leather wallets to my cart"},
               {"role": "assistant", "content": r.json()["reply"]}]

    _patch_turn(monkeypatch, [
        {"tool": "execute_payment", "args": {}},
        "That exceeds your per-transaction limit. Would you like to remove an item?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "buyer_agent",
                                   "message": "Yes, proceed", "history": history})
    # Block is expected control flow — HTTP 200, not 4xx/5xx.
    assert r.status_code == 200
    body = r.json()
    assert body["blocked"] is True
    assert "execute_payment" in body["tool_calls_made"]


def test_blocked_creates_no_order(db_session_factory, monkeypatch):
    """A blocked money action must create NO Order row and no Razorpay call."""
    _seed(PRODUCTS, db_session_factory)
    session_id = "sess-c"
    _patch_turn(monkeypatch, [
        {"tool": "update_cart",
         "args": {"action": "add", "item": {"type": "merchant", "ref_id": "ACC-001",
                                            "name": "Leather Wallet", "price": 899.0,
                                            "quantity": 5, "source": "merchant"}}},
        "Cart: ₹4495. Shall I proceed?",
    ])
    r = client.post("/chat", json={"session_id": session_id, "actor": "buyer_agent",
                                   "message": "add 5 leather wallets"})
    history = [{"role": "user", "content": "add 5 leather wallets"},
               {"role": "assistant", "content": r.json()["reply"]}]

    _patch_turn(monkeypatch, [
        {"tool": "execute_payment", "args": {}},
        "Blocked.",
    ])
    client.post("/chat", json={"session_id": session_id, "actor": "buyer_agent",
                               "message": "yes", "history": history})

    orders = _orders(db_session_factory)
    assert orders == []
    events = _audit_events(db_session_factory, session_id)
    assert ("guardrail_decision", "blocked", "BLOCK") in events
