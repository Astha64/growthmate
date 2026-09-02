"""
SQLAlchemy models for GrowthMate — Revision 2.

Implements LOW_LEVEL_DESIGN.md §2 exactly:
  - Product                -> products (merchant catalog, upsell source only)
  - ExternalProductListing -> external_product_listings (live discovery)
  - CartItem               -> cart_items
  - Order                  -> orders (multi-item, no single product FK)
  - OrderItem              -> order_items (frozen cart snapshot)
  - CartEvent              -> cart_events (growth analytics)
  - AuditLog               -> audit_log (expanded event_type taxonomy)

BREAKING SCHEMA CHANGE from Revision 1:
  - Order: removed product_id FK, replaced `amount` with subtotal + total
  - AuditLog: replaced `guardrail_decision`/`guardrail_reason` with
    `event_type`/`decision`/`reason`
  - CartEvent: replaced `product_id` FK with `ref_id` string
  - After writing this file, delete growthmate.db and re-run seed_data.py.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# §2.1  Product — merchant catalog. Upsell/cross-sell source only.
# ---------------------------------------------------------------------------

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    stock = Column(Integer, nullable=False, default=0)
    category = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.2  ExternalProductListing — live-discovered candidate.
# ---------------------------------------------------------------------------

class ExternalProductListing(Base):
    __tablename__ = "external_product_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    source_url = Column(Text, nullable=True)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    brand = Column(String(100), nullable=True)
    features_json = Column(Text, nullable=True)
    rating = Column(Float, nullable=True)
    availability = Column(String(30), nullable=True)
    dedup_group_id = Column(Integer, nullable=True, index=True)
    extracted_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.3  CartItem — mixed-type cart (external + merchant items).
# ---------------------------------------------------------------------------

class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    item_type = Column(String(20), nullable=False)  # "external" | "merchant"
    ref_id = Column(String(100), nullable=False)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    source = Column(String(50), nullable=True)
    added_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.4  Order / OrderItem — multi-item, no single-product FK.
# ---------------------------------------------------------------------------

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    razorpay_order_id = Column(String(100), nullable=True, index=True)
    razorpay_payment_link_id = Column(String(100), nullable=True, index=True)
    actor = Column(String(50), nullable=False)
    session_id = Column(String(100), nullable=False, index=True)
    subtotal = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    status = Column(String(30), nullable=False, default="created")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    source = Column(String(50), nullable=True)
    ref_id = Column(String(100), nullable=True)

    order = relationship("Order", back_populates="items")


# ---------------------------------------------------------------------------
# §2.5  CartEvent — growth analytics breadcrumbs (unchanged from Rev 1).
# ---------------------------------------------------------------------------

class CartEvent(Base):
    __tablename__ = "cart_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    ref_id = Column(String(100), nullable=True)
    event_type = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.5  AuditLog — expanded event_type taxonomy (LLD §2.5).
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    event_type = Column(String(50), nullable=False, index=True)
    tool_name = Column(String(50), nullable=True)
    parameters_json = Column(Text, nullable=True)
    agent_reasoning = Column(Text, nullable=True)
    decision = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    outcome = Column(String(20), nullable=False)
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)
