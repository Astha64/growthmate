"""
Tool implementations for GrowthMate's agent.

Each function maps to a JSON schema in LOW_LEVEL_DESIGN.md §4 and returns a
plain JSON-serializable dict — never raises. Per §11.3, create_payment_link
owns the full multi-step flow (product lookup -> Order insert -> Razorpay
call -> update/Order status) using the computed_amount passed from the
guardrail node.
"""

import json
from datetime import datetime, timezone

from sqlalchemy import func

from app import db as db_module
from app.models import AuditLog, CartEvent, Order, Product
from app.razorpay_client import create_payment_link as rzp_create_payment_link


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM-facing JSON schemas, matching LOW_LEVEL_DESIGN.md §4 exactly.
TOOLS = [
    {
        "name": "search_catalog",
        "description": "Search the product catalog by keyword and optional max price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_price": {"type": "number"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_payment_link",
        "description": "Create a Razorpay payment link for a specific product and quantity. Money-moving — subject to guardrail check.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
            },
            "required": ["sku", "quantity"],
        },
    },
    {
        "name": "get_order_status",
        "description": "Check the payment/order status for a given order id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "get_growth_insights",
        "description": "Return aggregate growth data: top products, abandonment patterns.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def search_catalog(query: str, max_price: float | None = None) -> dict:
    """Search the product catalog by keyword and optional max price."""
    db = db_module.SessionLocal()
    try:
        q = db.query(Product)
        if query:
            like = f"%{query.lower()}%"
            q = q.filter(
                (func.lower(Product.name).like(like))
                | (func.lower(Product.category).like(like))
                | (func.lower(Product.description).like(like))
            )
        if max_price is not None:
            q = q.filter(Product.price <= max_price)
        results = q.limit(20).all()
        products = [
            {
                "sku": p.sku,
                "name": p.name,
                "description": p.description,
                "price": p.price,
                "currency": p.currency,
                "stock": p.stock,
                "category": p.category,
            }
            for p in results
        ]
        _log_cart_event(db, session_id="", actor="", product_id=None, event_type="searched")
        return {"query": query, "max_price": max_price, "count": len(products), "products": products}
    finally:
        db.close()


def get_order_status(order_id: int) -> dict:
    """Check the payment/order status for a given order id."""
    db = db_module.SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {"error": f"order {order_id} not found"}
        return {
            "order_id": order.id,
            "status": order.status,
            "amount": order.amount,
            "currency": order.currency,
            "razorpay_payment_link_id": order.razorpay_payment_link_id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
    finally:
        db.close()


def get_growth_insights() -> dict:
    """Return aggregate growth data: top products, abandonment patterns."""
    db = db_module.SessionLocal()
    try:
        top = (
            db.query(Product, func.count(Order.id))
            .join(Order, Order.product_id == Product.id)
            .group_by(Product.id)
            .order_by(func.count(Order.id).desc())
            .limit(5)
            .all()
        )
        top_products = [
            {"sku": p.sku, "name": p.name, "orders": n}
            for p, n in top
        ]

        searched = set(
            row[0]
            for row in db.query(CartEvent.product_id)
            .filter(CartEvent.event_type == "searched")
            .all()
        )
        purchased = set(
            row[0]
            for row in db.query(CartEvent.product_id)
            .filter(CartEvent.event_type == "purchased")
            .all()
        )
        abandoned_skus = list(searched - purchased)

        abandoned = []
        if abandoned_skus:
            for pid in abandoned_skus:
                p = db.query(Product).filter(Product.id == pid).first()
                if p:
                    abandoned.append({"sku": p.sku, "name": p.name})

        return {
            "top_products": top_products,
            "abandonment": {"count": len(abandoned), "skus": [a["sku"] for a in abandoned]},
        }
    finally:
        db.close()


def create_payment_link(
    sku: str,
    quantity: int,
    actor: str,
    session_id: str,
    computed_amount: float | None = None,
) -> dict:
    """
    Full multi-step flow per LLD §11.3. Uses computed_amount passed in from the
    guardrail node rather than recomputing (avoids a race between quote & exec).
    Never raises.
    """
    db = db_module.SessionLocal()
    try:
        product = db.query(Product).filter(Product.sku == sku).first()
        if not product:
            return {"error": f"product with sku '{sku}' not found"}
        if quantity < 1 or quantity > product.stock:
            return {"error": f"invalid quantity {quantity} (stock: {product.stock})"}

        amount = computed_amount if computed_amount is not None else round(product.price * quantity, 2)
        currency = product.currency

        order = Order(
            product_id=product.id,
            actor=actor,
            session_id=session_id,
            amount=amount,
            currency=currency,
            status="created",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        result = rzp_create_payment_link(order)

        if "error" in result:
            order.status = "failed"
            db.commit()
            return {"error": result["error"]}

        order.razorpay_payment_link_id = result.get("id")
        order.razorpay_order_id = result.get("order_id")
        db.commit()

        _log_cart_event(db, session_id, actor, product.id, "purchased")

        return {
            "short_url": result.get("short_url"),
            "order_id": order.id,
            "amount": amount,
            "currency": currency,
            "sku": sku,
            "quantity": quantity,
        }
    finally:
        db.close()


# --- internal helpers (not LLM tools) ---


def _log_cart_event(db, session_id: str, actor: str, product_id: int | None, event_type: str) -> None:
    try:
        if not session_id or not actor:
            return
        db.add(
            CartEvent(
                session_id=session_id,
                actor=actor,
                product_id=product_id,
                event_type=event_type,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
