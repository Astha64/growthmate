"""
Tool tests — Revision 2. Use the in-memory DB fixture (§11.8) and monkeypatch
the Razorpay wrapper and external product sources — no real network call.

Covers:
  - discover_and_recommend_products (discovery pipeline with mocked sources)
  - recommend_complementary_products (merchant upsell)
  - update_cart + prepare_checkout (deterministic backend-computed total)
  - execute_payment + get_payment_status
  - get_growth_insights
"""

from app import tools
from app.models import CartItem, Order, OrderItem, Product


def _seed_merchant(db_session):
    s = db_session()
    s.add_all(
        [
            Product(sku="SHOE-001", name="Nike Revolution 6", description="Running shoe",
                    price=1899.0, currency="INR", stock=20, category="footwear"),
            Product(sku="APP-001", name="Cotton Crew T-Shirt", description="Crew tee",
                    price=499.0, currency="INR", stock=50, category="apparel"),
            Product(sku="ACC-001", name="Leather Wallet", description="Bifold wallet",
                    price=899.0, currency="INR", stock=22, category="accessories"),
            Product(sku="ELEC-001", name="Wireless Earbuds", description="Audio",
                    price=1999.0, currency="INR", stock=30, category="electronics"),
        ]
    )
    s.commit()
    s.close()


# ---------------------------------------------------------------------------
# Discovery — mocked external sources.
# ---------------------------------------------------------------------------

def _raw_candidates():
    # Two sources; the Pegasus appears in both (should dedupe to one).
    return [
        {"source_url": "https://a.test/p1", "title": "Nike Pegasus 40", "price": 2100,
         "currency": "INR", "brand": "Nike", "features": ["running", "road"], "rating": 4.5,
         "availability": "in_stock"},
        {"source_url": "https://a.test/p2", "title": "Adidas Ultraboost", "price": 3600,
         "currency": "INR", "brand": "Adidas", "features": ["running"], "rating": 4.7,
         "availability": "in_stock"},
        {"source_url": "https://a.test/p3", "title": "Asics Kayano", "price": 2450,
         "currency": "INR", "brand": "Asics", "features": ["running", "stability"], "rating": 4.4,
         "availability": "in_stock"},
        # duplicate of the first listing (different source) — must dedupe
        {"source_url": "https://b.test/p1", "title": "Nike Pegasus 40", "price": 2050,
         "currency": "INR", "brand": "Nike", "features": ["running", "road"], "rating": 4.6,
         "availability": "in_stock"},
    ]


def test_discover_and_recommend_products_top3(db_session_factory, monkeypatch):
    import app.discovery as discovery
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: _raw_candidates())
    res = tools.discover_and_recommend_products({"budget": 3000, "required_features": ["running"]})
    assert "error" not in res
    # Ultraboost (₹3600) is over budget, so only 2 candidates survive filtering
    # after dedup — the pipeline returns min(3, available).
    assert res["count"] == len(res["recommendations"]) == 2
    # The duplicate Pegasus must appear only once (dedup).
    names = [r["name"] for r in res["recommendations"]]
    assert names.count("Nike Pegasus 40") == 1
    # Every recommendation carries an explainable 'why' and a price.
    for rec in res["recommendations"]:
        assert rec["why"]
        assert rec["price"] > 0


def test_discover_and_recommend_products_respects_budget(db_session_factory, monkeypatch):
    import app.discovery as discovery
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: _raw_candidates())
    res = tools.discover_and_recommend_products({"budget": 2200, "required_features": ["running"]})
    for rec in res["recommendations"]:
        assert rec["price"] <= 2200  # hard-constraint filtering applied


def test_discover_and_recommend_products_degrades_on_source_error(db_session_factory, monkeypatch):
    import app.discovery as discovery

    def boom(req):
        raise RuntimeError("source down")
    monkeypatch.setattr(discovery, "search_external_sources", boom)
    # The tool wrapper must not raise — it degrades to a clean error dict.
    res = tools.discover_and_recommend_products({"budget": 3000})
    assert "error" in res


# ---------------------------------------------------------------------------
# Merchant upsell.
# ---------------------------------------------------------------------------

def test_recommend_complementary_products(db_session_factory):
    _seed_merchant(db_session_factory)
    res = tools.recommend_complementary_products(
        {"name": "Nike Revolution 6", "price": 1899.0, "category": "footwear"}
    )
    assert "error" not in res
    assert 1 <= len(res["candidates"]) <= 3
    # Merchant catalog is upsell-only: footwear adjacency -> apparel/accessories.
    cats = {c["category"] for c in res["candidates"]}
    assert cats & {"apparel", "accessories"}


# ---------------------------------------------------------------------------
# Cart & checkout — deterministic backend-computed total.
# ---------------------------------------------------------------------------

def test_update_cart_add_and_total(db_session_factory):
    _seed_merchant(db_session_factory)
    res = tools.update_cart("add", {
        "session_id": "s1", "actor": "human", "type": "external",
        "ref_id": "ext1", "name": "Nike Pegasus 40", "price": 2050.0,
        "quantity": 1, "source": "https://a.test/p1",
    })
    assert res["result"] == "ok"
    assert res["subtotal"] == 2050.0 and res["total"] == 2050.0

    res2 = tools.update_cart("add", {
        "session_id": "s1", "actor": "human", "type": "merchant",
        "ref_id": "APP-001", "name": "Cotton Crew T-Shirt", "price": 499.0,
        "quantity": 2, "source": "merchant",
    })
    # Total is backend-recomputed, never carried from a stale number.
    assert res2["total"] == round(2050.0 + 2 * 499.0, 2)


def test_update_cart_remove_and_set_quantity(db_session_factory):
    _seed_merchant(db_session_factory)
    tools.update_cart("add", {"session_id": "s1", "actor": "human", "type": "external",
                              "ref_id": "ext1", "name": "A", "price": 100.0, "quantity": 1, "source": "s"})
    tools.update_cart("add", {"session_id": "s1", "actor": "human", "type": "external",
                              "ref_id": "ext2", "name": "B", "price": 200.0, "quantity": 1, "source": "s"})
    res = tools.update_cart("set_quantity", {"session_id": "s1", "actor": "human", "type": "external",
                                             "ref_id": "ext2", "name": "B", "price": 200.0, "quantity": 3, "source": "s"})
    assert res["total"] == round(100.0 + 3 * 200.0, 2)
    res = tools.update_cart("remove", {"session_id": "s1", "actor": "human", "type": "external",
                                       "ref_id": "ext1", "name": "A", "price": 100.0, "quantity": 1, "source": "s"})
    assert res["total"] == 600.0


def test_prepare_checkout_recomputes_total_never_passed_through(db_session_factory):
    """§4.3/§9: prepare_checkout recomputes from DB rows — it takes only a
    session_id and ignores any supplied total (there is no total argument)."""
    _seed_merchant(db_session_factory)
    tools.update_cart("add", {"session_id": "s1", "actor": "human", "type": "merchant",
                              "ref_id": "APP-001", "name": "Cotton Crew T-Shirt", "price": 499.0,
                              "quantity": 4, "source": "merchant"})
    preview = tools.prepare_checkout("s1")
    assert preview["total"] == 4 * 499.0  # recomputed: 1996.0


def test_prepare_checkout_empty_cart(db_session_factory):
    _seed_merchant(db_session_factory)
    preview = tools.prepare_checkout("s1")
    assert preview["items"] == []
    assert preview["total"] == 0.0


# ---------------------------------------------------------------------------
# Payment.
# ---------------------------------------------------------------------------

def _seed_cart(db_session, session_id="s1"):
    s = db_session()
    s.add(CartItem(session_id=session_id, item_type="merchant", ref_id="APP-001",
                   name="Cotton Crew T-Shirt", price=499.0, quantity=2, source="merchant"))
    s.commit()
    s.close()


def test_execute_payment_success_snapshots_cart(db_session_factory, monkeypatch):
    _seed_merchant(db_session_factory)
    _seed_cart(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link",
                        lambda o: {"id": "plink_1", "short_url": "https://rzp.test/link", "order_id": "ord_1"})
    res = tools.execute_payment(session_id="s1", actor="human")
    assert "error" not in res
    assert res["amount"] == 998.0  # 2 x 499, backend-computed

    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "s1").first()
    assert order is not None
    assert order.total == 998.0
    assert order.subtotal == 998.0
    items = s.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    s.close()
    assert len(items) == 1
    assert items[0].name == "Cotton Crew T-Shirt"
    assert items[0].quantity == 2


def test_execute_payment_empty_cart_fails(db_session_factory, monkeypatch):
    _seed_merchant(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda o: {"id": "x", "short_url": "s", "order_id": "o"})
    res = tools.execute_payment(session_id="s-empty", actor="human")
    assert "error" in res


def test_execute_payment_rzp_failure_marks_order_failed(db_session_factory, monkeypatch):
    _seed_merchant(db_session_factory)
    _seed_cart(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda order: {"error": "boom"})
    res = tools.execute_payment(session_id="s1", actor="human")
    assert "error" in res and res["error"] == "boom"
    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "s1").first()
    s.close()
    assert order.status == "failed"


def test_get_payment_status_found_and_missing(db_session_factory, monkeypatch):
    _seed_merchant(db_session_factory)
    _seed_cart(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link",
                        lambda o: {"id": "plink_1", "short_url": "s", "order_id": "o"})
    created = tools.execute_payment(session_id="s1", actor="human")
    status = tools.get_payment_status(order_id=created["order_id"])
    assert status["status"] == "created"
    assert status["total"] == 998.0
    assert len(status["items"]) == 1
    assert tools.get_payment_status(order_id=9999)["error"]


def test_get_growth_insights_returns_shape(db_session_factory, monkeypatch):
    _seed_merchant(db_session_factory)
    _seed_cart(db_session_factory)
    monkeypatch.setattr(tools, "rzp_create_payment_link", lambda o: {"id": "x", "short_url": "s", "order_id": "o"})
    tools.execute_payment(session_id="s1", actor="human")
    res = tools.get_growth_insights()
    assert "top_products" in res and "abandonment" in res
