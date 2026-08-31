"""
Chat endpoint tests. Uses FastAPI TestClient; monkeypatches the LLM call inside
agent_node so no real Anthropic API key or network is required (LLD §11.8).

Covers:
  (a) a read-only tool flow returns tool_calls_made correctly
  (b) a blocked money action returns HTTP 200 with blocked=true
  (c) AuditLog gains exactly one row per /chat request
"""

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app import agent_graph
from app.main import app
from app.models import AuditLog, Product

client = TestClient(app)


class ScriptedLLM:
    """Returns a tool-call AIMessage, then a plain-text AIMessage.

    The instance persists across agent_node calls (agent_node calls _make_llm
    fresh each time, so the SAME instance must be returned to keep the call
    counter and terminate the loop: tool_call first, then final text).
    """

    def __init__(self, tool_name, tool_args, final_text):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.final_text = final_text
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": self.tool_args,
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=self.final_text)


def _patch_llm(monkeypatch, tool_name, tool_args, final_text):
    """Monkeypatch _make_llm to always return the same persistent ScriptedLLM."""
    scripted = ScriptedLLM(tool_name, tool_args, final_text)
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


def _audit_rows(db_session_factory):
    s = db_session_factory()
    rows = s.query(AuditLog).all()
    s.close()
    return rows


PRODUCTS = [
    ("SHOE-001", "Nike Revolution 6", 1899.0, 20, "footwear"),
    ("APP-001", "Cotton Crew T-Shirt", 499.0, 50, "apparel"),
]


def test_read_only_tool_flow(db_session_factory, monkeypatch):
    _seed(PRODUCTS, db_session_factory)
    _patch_llm(monkeypatch, "search_catalog", {"query": "shoe"}, "We have the Nike Revolution 6.")
    resp = client.post(
        "/chat",
        json={"session_id": "sess-1", "actor": "human", "message": "find shoes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is False
    assert "search_catalog" in body["tool_calls_made"]
    assert "Nike" in body["reply"]

    # (c) exactly one AuditLog row per request
    rows = _audit_rows(db_session_factory)
    assert len(rows) == 1
    assert rows[0].tool_name == "search_catalog"
    assert rows[0].guardrail_decision == "N/A"
    assert rows[0].outcome == "success"


def test_blocked_money_action_returns_200(db_session_factory, monkeypatch):
    # APP-001 x10 = 4990 > buyer_agent per-transaction limit of 3000 (Journey B)
    _seed(PRODUCTS, db_session_factory)
    _patch_llm(
        monkeypatch,
        "create_payment_link",
        {"sku": "APP-001", "quantity": 10},
        "I'm sorry, that purchase exceeds the per-transaction limit.",
    )

    resp = client.post(
        "/chat",
        json={"session_id": "sess-b", "actor": "buyer_agent", "message": "buy 10 shirts"},
    )
    # Block is expected control flow — must be HTTP 200, not 4xx/5xx.
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert "create_payment_link" in body["tool_calls_made"]

    rows = _audit_rows(db_session_factory)
    assert len(rows) == 1
    assert rows[0].guardrail_decision == "BLOCK"
    assert rows[0].outcome == "blocked"
    assert "per-transaction limit" in (rows[0].guardrail_reason or "")


def test_blocked_creates_no_order(db_session_factory, monkeypatch):
    """§11.5: a blocked money action must create NO Order row / no Razorpay call."""
    from app.models import Order

    _seed(PRODUCTS, db_session_factory)
    _patch_llm(
        monkeypatch,
        "create_payment_link",
        {"sku": "APP-001", "quantity": 10},
        "Blocked.",
    )
    client.post(
        "/chat",
        json={"session_id": "sess-c", "actor": "buyer_agent", "message": "buy 10"},
    )
    s = db_session_factory()
    orders = s.query(Order).all()
    s.close()
    assert orders == []
