"""
Pydantic request/response models for GrowthMate's REST API.

Matches LOW_LEVEL_DESIGN.md §3 (REST API Contract).
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class ProductOut(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    price: float
    stock: int
    category: Optional[str] = None


class CatalogResponse(BaseModel):
    currency: str
    products: List[ProductOut]


class ChatRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")
    message: str
    # Optional prior conversation turns, as [{"role": ..., "content": ...}],
    # used to reconstruct multi-turn agent state across /chat invocations
    # (LLD §3 — "optional history"). Defaults to empty.
    history: Optional[List[dict]] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls_made: List[str] = []
    blocked: bool = False


class AuditLogOut(BaseModel):
    id: int
    session_id: str
    actor: str
    event_type: str
    tool_name: Optional[str] = None
    parameters_json: Optional[str] = None
    agent_reasoning: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    outcome: str
    error_detail: Optional[str] = None
    created_at: str


class WebhookResponse(BaseModel):
    status: str
