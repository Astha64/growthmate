"""
GrowthMate FastAPI routes — routing only, no business logic (LLD §10).

Endpoints:
  GET  /health            liveness
  GET  /catalog           agent-readable catalog (LLD §3)
  POST /chat              agent conversation (human or buyer_agent)
  GET  /audit             audit trail (LLD §3)
  POST /webhook/razorpay  Razorpay payment-status webhook (§11.6)

Per §11.7, spend_so_far is computed fresh from the DB at the start of each
/chat call (state is not long-lived across requests). The webhook reads the
raw body via await request.body() for HMAC verification (LLD §7 / §11.6).
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
    """Agent-readable catalog: explicit fields + currency, no formatting (LLD §3)."""
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
    buyer_agent actors. Money actions are gated by guardrail_node; blocks return
    HTTP 200 with blocked=true (expected control flow, not an error).
    """
    # §11.7: compute spend_so_far fresh from DB, excluding blocked/failed orders.
    db = db_module.SessionLocal()
    try:
        spend_so_far = (
            db.query(func.coalesce(func.sum(Order.amount), 0.0))
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

    initial_state = {
        "messages": [{"role": "user", "content": req.message}],
        "actor": req.actor,
        "session_id": req.session_id,
        "spend_so_far": float(spend_so_far),
        "pending_tool_call": None,
        "computed_amount": None,
        "last_decision": None,
        "last_decision_reason": None,
        "tool_result": None,
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
        blocked=blocked,
    )


def _extract_reply(state: dict) -> str:
    """Return the last assistant message that is plain text (not part of a tool call)."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "assistant" and msg.get("content"):
            return msg["content"]
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
            tool_name=r.tool_name,
            parameters_json=r.parameters_json,
            agent_reasoning=r.agent_reasoning,
            guardrail_decision=r.guardrail_decision,
            guardrail_reason=r.guardrail_reason,
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
