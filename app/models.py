"""
SQLAlchemy models for GrowthMate.

Maps exactly to LOW_LEVEL_DESIGN.md §2 (Database Schema):
  - Product    -> products
  - Order      -> orders
  - CartEvent  -> cart_events
  - AuditLog   -> audit_log
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    orders = relationship("Order", back_populates="product")
    cart_events = relationship("CartEvent", back_populates="product")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    razorpay_order_id = Column(String(100), nullable=True, index=True)
    razorpay_payment_link_id = Column(String(100), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    actor = Column(String(50), nullable=False)
    session_id = Column(String(100), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(30), nullable=False, default="created")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    product = relationship("Product", back_populates="orders")


class CartEvent(Base):
    __tablename__ = "cart_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    event_type = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    product = relationship("Product", back_populates="cart_events")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    tool_name = Column(String(50), nullable=False)
    parameters_json = Column(Text, nullable=False)
    agent_reasoning = Column(Text, nullable=True)
    guardrail_decision = Column(String(20), nullable=False)
    guardrail_reason = Column(Text, nullable=True)
    outcome = Column(String(20), nullable=False)
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)
