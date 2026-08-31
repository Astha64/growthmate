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


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls_made: List[str] = []
    blocked: bool = False


class AuditLogOut(BaseModel):
    id: int
    session_id: str
    actor: str
    tool_name: str
    parameters_json: str
    agent_reasoning: Optional[str]
    guardrail_decision: str
    guardrail_reason: Optional[str]
    outcome: str
    error_detail: Optional[str]
    created_at: str


class WebhookResponse(BaseModel):
    status: str
