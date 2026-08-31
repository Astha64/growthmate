"""
Tool tests. Use the in-memory DB fixture (§11.8) and monkeypatch the Razorpay
wrapper — no real network call in any test.
"""

from app import tools
from app.models import Product


def _seed(db_session):
    s = db_session()
    s.add_all(
        [
            Product(sku="SHOE-001", name="Nike Revolution 6", description="Running shoe",
                    price=1899.0, currency="INR", stock=20, category="footwear"),
            Product(sku="APP-001", name="Cotton Crew T-Shirt", description="Crew tee",
                    price=499.0, currency="INR", stock=50, category="apparel"),
            Product(sku="ELEC-001", name="Wireless Earbuds", description="Audio",
                    price=1999.0, currency="INR", stock=30, category="electronics"),
        ]
    )
    s.commit()
    s.close()


def test_search_catalog_by_name(db_session_factory):
    _seed(db_session_factory)
    res = tools.search_catalog(query="nike")
    assert res["count"] == 1
    assert res["products"][0]["sku"] == "SHOE-001"
    assert res["products"][0]["price"] == 1899.0


def test_search_catalog_by_category(db_session_factory):
    _seed(db_session_factory)
    res = tools.search_catalog(query="apparel")
    assert res["count"] == 1
    assert res["products"][0]["sku"] == "APP-001"


def test_search_catalog_with_max_price(db_session_factory):
    _seed(db_session_factory)
    res = tools.search_catalog(query="", max_price=1000.0)
    skus = {p["sku"] for p in res["products"]}
    assert "APP-001" in skus
    assert "SHOE-001" not in skus


def test_create_payment_link_success(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(
        tools,
        "rzp_create_payment_link",
        lambda order: {"id": "plink_123", "short_url": "https://rzp.test/link", "order_id": "ord_1"},
    )
    res = tools.create_payment_link(
        sku="SHOE-001", quantity=1, actor="human", session_id="s1",
        computed_amount=1899.0,
    )
    assert "error" not in res
    assert res["order_id"] == 1
    assert res["short_url"] == "https://rzp.test/link"


def test_create_payment_link_uses_computed_amount(db_session_factory, monkeypatch):
    """§11.3/11.5: tools must use the guardrail's computed_amount, not recompute."""
    _seed(db_session_factory)
    captured = {}

    def fake_rzp(order):
        captured["amount"] = order.amount
        return {"id": "plink_x", "short_url": "s", "order_id": "o"}

    monkeypatch.setattr(tools, "rzp_create_payment_link", fake_rzp)
    tools.create_payment_link(
        sku="SHOE-001", quantity=1, actor="human", session_id="s1",
        computed_amount=999.99,
    )
    assert captured["amount"] == 999.99


def test_create_payment_link_unknown_sku(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda o: {"id": "x", "short_url": "s", "order_id": "o"})
    res = tools.create_payment_link(sku="NOPE", quantity=1, actor="human", session_id="s1", computed_amount=10.0)
    assert "error" in res
    assert "not found" in res["error"]


def test_create_payment_link_insufficient_stock(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda o: {"id": "x", "short_url": "s", "order_id": "o"})
    res = tools.create_payment_link(sku="SHOE-001", quantity=99, actor="human", session_id="s1", computed_amount=999.0)
    assert "error" in res


def test_create_payment_link_rzp_failure_marks_order_failed(db_session_factory, monkeypatch):
    """Razorpay SDK exception -> outcome failed, Order marked failed, no raise."""
    _seed(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda order: {"error": "boom"})

    res = tools.create_payment_link(
        sku="APP-001", quantity=1, actor="human", session_id="s1", computed_amount=499.0,
    )
    assert "error" in res and res["error"] == "boom"


def test_get_order_status_found_and_missing(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(
        tools, "rzp_create_payment_link",
        lambda o: {"id": "plink_1", "short_url": "s", "order_id": "o"},
    )
    created = tools.create_payment_link(sku="ELEC-001", quantity=1, actor="human", session_id="s1", computed_amount=1999.0)
    status = tools.get_order_status(order_id=created["order_id"])
    assert status["status"] == "created"
    assert tools.get_order_status(order_id=9999)["error"]


def test_get_growth_insights_returns_shape(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda o: {"id": "x", "short_url": "s", "order_id": "o"})
    tools.create_payment_link(sku="ELEC-001", quantity=1, actor="human", session_id="s1", computed_amount=1999.0)
    res = tools.get_growth_insights()
    assert "top_products" in res and "abandonment" in res
