"""
GrowthMate FastAPI routes — routing only, no business logic (LLD §10).

Endpoints:
  GET  /health            liveness
  GET  /catalog           merchant catalog (upsell source, LLD §3)
  POST /chat              agent conversation (human or buyer_agent)
  GET  /audit             audit trail (LLD §3)
  POST /webhook/razorpay  Razorpay payment-status webhook (§11.6)

spend_so_far is computed fresh from the DB at the start of each /chat call
(state is not long-lived across requests). Revision 2: sums Order.total
(multi-item) instead of the old single-product Order.amount.

To support a multi-turn pipeline (clarification -> discovery -> ... -> approval),
the /chat endpoint also reconstructs the session's shown checkout preview (and
its backend-computed total) from the current cart_items rows when the cart is
non-empty. This is what lets approval_node fail-closed on "a preview was shown"
across separate /chat invocations — see LLD §5 / §6.1.
"""

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import db as db_module
from app.agent_graph import graph
from app.db import get_db, init_db
from app.models import AuditLog, Order, Product
from app.razorpay_client import verify_webhook_signature
from app.schemas import (
    AuditLogOut,
    CatalogResponse,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    ProductOut,
    WebhookResponse,
)
from app.tools import prepare_checkout

app = FastAPI(title="GrowthMate API", version="0.1.0")

init_db()

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def root():
    return {"message": "GrowthMate API is running. See /docs for API docs."}


@app.get("/health", response_model=HealthResponse)
def health_check():
    return HealthResponse(status="ok", service="growthmate-backend")


@app.get("/catalog", response_model=CatalogResponse)
def get_catalog(db: Session = Depends(get_db)):
    """Merchant catalog: explicit fields + currency, no formatting (LLD §3)."""
    products = db.query(Product).all()
    return CatalogResponse(
        currency="INR",
        products=[
            ProductOut(
                sku=p.sku,
                name=p.name,
                description=p.description,
                price=p.price,
                stock=p.stock,
                category=p.category,
            )
            for p in products
        ],
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    Runs the LangGraph agent loop for the given message. Accepts both human and
    buyer_agent actors. Money actions are gated by approval_node then
    guardrail_node; blocks return HTTP 200 with blocked=true (expected control
    flow, not an error).
    """
    # Spend so far: fresh from DB, excluding blocked/failed orders. Rev 2 sums
    # Order.total (multi-item) rather than the old single-item Order.amount.
    db = db_module.SessionLocal()
    try:
        spend_so_far = (
            db.query(func.coalesce(func.sum(Order.total), 0.0))
            .filter(
                Order.session_id == req.session_id,
                Order.actor == req.actor,
                Order.status.in_(["created", "paid"]),
            )
            .scalar()
            or 0.0
        )
    finally:
        db.close()

    # Reconstruct messages from optional history + the current message.
    messages = list(req.history or [])
    messages.append({"role": "user", "content": req.message})

    # Reconstruct the shown checkout preview (and backend total) for the session
    # if the cart is non-empty — the precondition approval_node checks (LLD §6.1).
    preview = prepare_checkout(req.session_id)
    checkout_preview = preview if preview and preview.get("items") else None
    cart_total = (checkout_preview or {}).get("total")

    initial_state = {
        "messages": messages,
        "actor": req.actor,
        "session_id": req.session_id,

        "structured_requirements": None,
        "requirements_complete": False,

        "discovery_results": None,
        "selected_product": None,
        "upsell_candidates": None,

        "cart": [],
        "cart_total": cart_total,
        "checkout_preview": checkout_preview,
        "approval_confirmed": False,

        "spend_so_far": float(spend_so_far),
        "computed_amount": cart_total,
        "last_decision": None,
        "last_decision_reason": None,

        "pending_tool_call": None,
        "payment_state": None,
        "order_id": None,
        "tools_called": [],
    }

    final_state = graph.invoke(initial_state)

    reply = _extract_reply(final_state)
    tool_calls = _extract_tool_calls(final_state)
    blocked = final_state.get("last_decision") == "BLOCK"

    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        tool_calls_made=tool_calls,
        blocked=bool(blocked),
    )


def _extract_reply(state: dict) -> str:
    """Return the last assistant message that is plain text (not part of a tool call)."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        else:
            # LangChain AIMessage exposes .type ("ai") rather than .role; the
            # dictionary form uses role == "assistant". Accept both.
            role = getattr(msg, "role", None) or getattr(msg, "type", None)
            content = getattr(msg, "content", None)
        if role in ("assistant", "ai") and content:
            # content may be a str or a list of content blocks ('text' keys).
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    b if isinstance(b, str) else b.get("text", "")
                    for b in content
                    if isinstance(b, str) or (isinstance(b, dict) and b.get("text"))
                ]
                text = " ".join(parts).strip()
                if text:
                    return text
    return "I'm sorry, I couldn't complete that request."


def _extract_tool_calls(state: dict) -> list:
    # Recorded at the moment the agent requests each tool, so blocked calls
    # (which never reach a tool_ message) still appear in tool_calls_made (§3).
    return list(state.get("tools_called", []))


@app.get("/audit", response_model=list[AuditLogOut])
def get_audit(session_id: str | None = None, db: Session = Depends(get_db)):
    """Audit trail, most recent first. Optional session_id filter (LLD §3)."""
    q = db.query(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if session_id:
        q = q.filter(AuditLog.session_id == session_id)
    rows = q.limit(200).all()
    return [
        AuditLogOut(
            id=r.id,
            session_id=r.session_id,
            actor=r.actor,
            event_type=r.event_type,
            tool_name=r.tool_name,
            parameters_json=r.parameters_json,
            agent_reasoning=r.agent_reasoning,
            decision=r.decision,
            reason=r.reason,
            outcome=r.outcome,
            error_detail=r.error_detail,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@app.post("/webhook/razorpay", response_model=WebhookResponse)
async def razorpay_webhook(request: Request):
    """Razorpay payment-status webhook. Verifies HMAC on the raw body (LLD §7 / §11.6)."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="invalid signature")

    payload = json.loads(body)
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_link_id = entity.get("id")

    db = db_module.SessionLocal()
    try:
        if payment_link_id:
            order = (
                db.query(Order)
                .filter(Order.razorpay_payment_link_id == payment_link_id)
                .first()
            )
            if order:
                if "paid" in event:
                    order.status = "paid"
                elif "expired" in event:
                    order.status = "failed"
                db.commit()
        return WebhookResponse(status="processed")
    finally:
        db.close()
