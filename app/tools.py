"""
Tool implementations for GrowthMate's agent — Revision 2.

Each LLM-facing function maps to a JSON schema in LOW_LEVEL_DESIGN.md §4 and
returns a plain JSON-serializable dict — never raises.

Tool set (LLD §4):
  §4.1  discover_and_recommend_products   (delegates to app/discovery.py)
  §4.2  recommend_complementary_products  (merchant catalog upsell/cross-sell)
  §4.3  update_cart, prepare_checkout     (cart + deterministic total)
  §4.4  execute_payment, get_payment_status
  §4.5  get_growth_insights

Internal helpers (NOT LLM tools):
  - search_catalog         retained as an internal helper, called only from
                           recommend_complementary_products (ARCHITECTURE §10:
                           merchant catalog is upsell-only, never primary
                           discovery).
  - calculate_cart_total   internal, called inside update_cart and
                           prepare_checkout so the total shown is always
                           freshly computed from DB rows, never carried as a
                           stale or LLM-stated number (LLD §9).

Cart total is ALWAYS backend-computed (prepare_checkout / calculate_cart_total)
— never accepted from or trusted to the LLM, at any point (ARCHITECTURE §10).
"""

import json
from datetime import datetime, timezone

from sqlalchemy import func

from app import db as db_module
from app.discovery import discover_and_recommend_products as _discover
from app.models import AuditLog, CartEvent, CartItem, Order, OrderItem, Product
from app.razorpay_client import create_payment_link as rzp_create_payment_link


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# LLM-facing JSON schemas — matches LOW_LEVEL_DESIGN.md §4 exactly.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "discover_and_recommend_products",
        "description": (
            "Live, multi-source product discovery. Pass structured_requirements "
            "with 'budget' (number) and 'required_features'/'keywords' (list of "
            "strings). Returns the top 3 best-matching products with prices and "
            "a short 'why' for each. This is the PRIMARY discovery tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "structured_requirements": {"type": "object"},
            },
            "required": ["structured_requirements"],
        },
    },
    {
        "name": "recommend_complementary_products",
        "description": (
            "Suggest 2-3 complementary/cross-sell items from the merchant's own "
            "catalog that pair with the user's selected product. Call this AFTER "
            "the user has selected a discovery result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selected_product": {
                    "type": "object",
                    "description": "The product the user selected from discovery.",
                },
            },
            "required": ["selected_product"],
        },
    },
    {
        "name": "update_cart",
        "description": (
            "Add, remove, or set quantity of an item in the user's cart. "
            "'item' must contain type ('external'|'merchant'), ref_id, name, "
            "price, quantity, and source. The cart total is always "
            "recomputed server-side."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "set_quantity"]},
                "item": {"type": "object"},
            },
            "required": ["action", "item"],
        },
    },
    {
        "name": "prepare_checkout",
        "description": (
            "Compute the backend checkout preview (items, subtotal, total, "
            "currency) for the user's current session cart. The total is "
            "always recomputed from the cart — never trust a number you "
            "supplied. Call before requesting payment and before asking the "
            "user for explicit approval."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "execute_payment",
        "description": (
            "Execute payment for the current session cart against Razorpay "
            "(test mode). Money-moving — only call after the checkout preview "
            "was shown AND the user explicitly approved. The system performs "
            "approval validation and guardrail checks before executing."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_payment_status",
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


# ---------------------------------------------------------------------------
# §4.1  Discovery & Recommendation (delegates to internal pipeline).
# ---------------------------------------------------------------------------

def discover_and_recommend_products(structured_requirements: dict) -> dict:
    """/4.1 — single deterministic pipeline call (LLD §4.1, §7)."""
    try:
        return _discover(structured_requirements or {})
    except Exception as e:  # noqa: BLE001 - degrade to a clean error dict
        return {"error": f"discovery failed: {e}"}


# ---------------------------------------------------------------------------
# Internal helper: merchant catalog search (upsell-only, not primary discovery).
# ---------------------------------------------------------------------------

# LLD §8 — a small, explicit category-adjacency map for rule-based relatedness.
CATEGORY_ADJACENCY = {
    "footwear": ["apparel", "accessories"],
    "apparel": ["footwear", "accessories"],
    "electronics": ["accessories", "home"],
    "home": ["accessories", "electronics"],
    "accessories": ["apparel", "footwear", "electronics"],
}


def search_catalog(query: str, max_price: float | None = None) -> list[dict]:
    """Internal catalog search. Not exposed to the LLM — called only from
    recommend_complementary_products (ARCHITECTURE §10)."""
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
        return [
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
    finally:
        db.close()


# ---------------------------------------------------------------------------
# §4.2  Merchant Upsell / Cross-sell.
# ---------------------------------------------------------------------------

def recommend_complementary_products(selected_product: dict) -> dict:
    """Suggest 2-3 complementary items from the merchant catalog based on the
    selected product's category, using the category-adjacency map (LLD §8).
    """
    category = (selected_product or {}).get("category") or ""
    related_categories = CATEGORY_ADJACENCY.get(category, [])
    if not related_categories:
        # Fall back to any in-stock catalog items.
        related_categories = list(CATEGORY_ADJACENCY.keys())

    candidates: list[dict] = []
    seen_skus: set[str] = set()
    for rel_cat in related_categories:
        found = search_catalog(query=rel_cat)
        for item in found:
            if item["sku"] in seen_skus:
                continue
            if item["price"] <= 0:
                continue
            seen_skus.add(item["sku"])
            candidates.append(item)
        if len(candidates) >= 3:
            break

    return {
        "count": len(candidates),
        "candidates": candidates[:3],
        "based_on_category": category,
    }


# ---------------------------------------------------------------------------
# §4.3  Cart & Checkout — deterministic backend-computed total.
# ---------------------------------------------------------------------------

def _cart_items(session_id: str):
    db = db_module.SessionLocal()
    try:
        return db.query(CartItem).filter(CartItem.session_id == session_id).all()
    finally:
        db.close()


def calculate_cart_total(session_id: str) -> dict:
    """Compute subtotal/total from the current cart_items rows for the session.

    This is the ONLY source of truth for the cart total — never carried forward
    as a stale or LLM-stated number (LLD §9)."""
    db = db_module.SessionLocal()
    try:
        rows = db.query(CartItem).filter(CartItem.session_id == session_id).all()
        subtotal = round(sum(float(i.price) * int(i.quantity) for i in rows), 2)
        return {
            "subtotal": subtotal,
            "total": subtotal,  # no taxes/shipping in this scope; total == subtotal
            "currency": "INR",
            "count": sum(int(i.quantity) for i in rows),
        }
    finally:
        db.close()


def _log_internal_event(db, session_id: str, actor: str, event_type: str,
                        tool_name: str, parameters: dict, outcome: str,
                        decision=None, reason=None, error_detail=None) -> None:
    """Persist an AuditLog row for a tool-side event (direct tool call path)."""
    try:
        db.add(
            AuditLog(
                session_id=session_id,
                actor=actor,
                event_type=event_type,
                tool_name=tool_name,
                parameters_json=json.dumps(parameters),
                agent_reasoning=None,
                decision=decision,
                reason=reason,
                outcome=outcome,
                error_detail=error_detail,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def update_cart(action: str, item: dict) -> dict:
    """Mutate the cart_items rows for the session (LLD §4.3, §9).

    'item' keys: type ('external'|'merchant'), ref_id, name, price, quantity,
    source. Every mutation attempts to write a cart_update AuditLog row and a
    CartEvent breadcrumb. The response always carries the freshly recomputed
    cart totals.
    """
    session_id = str(item.get("session_id") or "")
    item_type = str(item.get("type") or "")
    ref_id = str(item.get("ref_id") or "")
    name = str(item.get("name") or "")
    price = float(item.get("price") or 0.0)
    quantity = int(item.get("quantity") or 1)
    source = item.get("source")
    actor = str(item.get("actor") or "")

    if action not in ("add", "remove", "set_quantity"):
        return {"error": f"unknown cart action: {action}", **calculate_cart_total(session_id)}

    db = db_module.SessionLocal()
    try:
        row = (
            db.query(CartItem)
            .filter(
                CartItem.session_id == session_id,
                CartItem.item_type == item_type,
                CartItem.ref_id == ref_id,
            )
            .first()
        )

        if action == "add":
            if row:
                row.quantity += quantity
            else:
                db.add(
                    CartItem(
                        session_id=session_id,
                        item_type=item_type,
                        ref_id=ref_id,
                        name=name,
                        price=price,
                        quantity=quantity,
                        source=source,
                    )
                )
        elif action == "set_quantity":
            if quantity <= 0:
                if row:
                    db.delete(row)
            elif row:
                row.quantity = quantity
                row.price = price
                row.name = name
            else:
                db.add(
                    CartItem(
                        session_id=session_id,
                        item_type=item_type,
                        ref_id=ref_id,
                        name=name,
                        price=price,
                        quantity=quantity,
                        source=source,
                    )
                )
        elif action == "remove":
            if row:
                db.delete(row)

        db.commit()
        _log_internal_event(
            db, session_id, actor, "cart_update", "update_cart",
            {"action": action, "item": item}, "success",
        )
        _log_cart_event(db, session_id, actor, ref_id, f"cart_{action}")
    finally:
        db.close()

    totals = calculate_cart_total(session_id)
    return {"action": action, "result": "ok", **totals}


def prepare_checkout(session_id: str) -> dict:
    """Backend checkout preview — always recomputes the total from DB rows
    (LLD §4.3, §9). Never trusts a total passed in by the LLM."""
    db = db_module.SessionLocal()
    try:
        rows = db.query(CartItem).filter(CartItem.session_id == session_id).all()
        items = [
            {
                "item_type": i.item_type,
                "ref_id": i.ref_id,
                "name": i.name,
                "price": i.price,
                "quantity": i.quantity,
                "source": i.source,
            }
            for i in rows
        ]
        subtotal = round(sum(float(i.price) * int(i.quantity) for i in rows), 2)
        return {
            "items": items,
            "subtotal": subtotal,
            "total": subtotal,
            "currency": "INR",
            "count": sum(int(i.quantity) for i in rows),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# §4.4  Approval & Payment.
# ---------------------------------------------------------------------------

def execute_payment(session_id: str, actor: str) -> dict:
    """Snapshot the cart into an Order + OrderItem rows, then call Razorpay.

    This is money-moving and must only be reached AFTER approval_node and
    guardrail_node have both passed (enforced by agent_graph routing, not
    re-checked here). On Razorpay failure the Order is marked 'failed' and the
    user is told they were not charged (LLD §10)."""
    db = db_module.SessionLocal()
    try:
        rows = db.query(CartItem).filter(CartItem.session_id == session_id).all()
        if not rows:
            return {"error": "cart is empty — nothing to charge"}

        currency = "INR"
        subtotal = round(sum(float(i.price) * int(i.quantity) for i in rows), 2)
        total = subtotal

        order = Order(
            actor=actor,
            session_id=session_id,
            subtotal=subtotal,
            total=total,
            currency=currency,
            status="created",
        )
        db.add(order)
        db.flush()

        for i in rows:
            db.add(
                OrderItem(
                    order_id=order.id,
                    name=i.name,
                    price=i.price,
                    quantity=i.quantity,
                    source=i.source,
                    ref_id=i.ref_id,
                )
            )
        db.commit()
        db.refresh(order)

        result = rzp_create_payment_link(order)

        if "error" in result:
            order.status = "failed"
            db.commit()
            return {"error": result["error"], **calculate_cart_total(session_id)}

        order.razorpay_payment_link_id = result.get("id")
        order.razorpay_order_id = result.get("order_id")
        db.commit()

        _log_cart_event(db, session_id, actor, None, "purchased")

        return {
            "short_url": result.get("short_url"),
            "order_id": order.id,
            "amount": total,
            "currency": currency,
            "razorpay_payment_link_id": result.get("id"),
        }
    finally:
        db.close()


def get_payment_status(order_id: int) -> dict:
    """Check the payment/order status for a given order id (LLD §4.4)."""
    db = db_module.SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {"error": f"order {order_id} not found"}
        items = [
            {
                "name": oi.name,
                "price": oi.price,
                "quantity": oi.quantity,
                "source": oi.source,
            }
            for oi in order.items
        ]
        return {
            "order_id": order.id,
            "status": order.status,
            "subtotal": order.subtotal,
            "total": order.total,
            "currency": order.currency,
            "items": items,
            "razorpay_payment_link_id": order.razorpay_payment_link_id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# §4.5  Growth.
# ---------------------------------------------------------------------------

def get_growth_insights() -> dict:
    """Aggregate growth data. Computed from multi-item Order/OrderItem rows."""
    db = db_module.SessionLocal()
    try:
        top = (
            db.query(OrderItem.name, func.sum(OrderItem.quantity))
            .group_by(OrderItem.name)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5)
            .all()
        )
        top_products = [{"name": n, "units": int(q)} for n, q in top]

        searched = set(
            row[0]
            for row in db.query(CartEvent.ref_id)
            .filter(CartEvent.event_type == "searched")
            .all()
        )
        purchased = set(
            row[0]
            for row in db.query(CartEvent.ref_id)
            .filter(CartEvent.event_type == "purchased")
            .all()
        )
        abandoned_ref_ids = list(searched - purchased)

        return {
            "top_products": top_products,
            "abandonment": {"count": len(abandoned_ref_ids), "ref_ids": abandoned_ref_ids},
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Internal helpers (not LLM tools).
# ---------------------------------------------------------------------------

def _log_cart_event(db, session_id: str, actor: str, ref_id: str | None, event_type: str) -> None:
    try:
        if not session_id or not actor:
            return
        db.add(
            CartEvent(
                session_id=session_id,
                actor=actor,
                ref_id=ref_id,
                event_type=event_type,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
