"""
LangGraph orchestration for GrowthMate. Matches LOW_LEVEL_DESIGN.md §6.

State (AgentState) uses the corrected schema from §11.2:
  messages / actor / session_id / spend_so_far / pending_tool_call
  + computed_amount / last_decision / last_decision_reason

Flow:
  agent_node --(conditional)--> END | guardrail | tool
  guardrail --(last_decision)--> tool (ALLOW) | audit (BLOCK)
  tool --> audit --> agent

Per §11.5, guardrail_node performs a single read-only DB lookup of the product
price to compute computed_amount = price * quantity before calling the guard
-- no writes, no Razorpay call.
"""

import json
import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from app import db as db_module
from app import guardrail as guardrail_module
from app.models import AuditLog, Product
from app.tools import TOOLS


class AgentState(TypedDict):
    messages: list
    actor: str
    session_id: str
    spend_so_far: float
    pending_tool_call: dict | None
    computed_amount: float | None
    last_decision: str | None
    last_decision_reason: str | None
    tool_result: dict | None
    tools_called: list


SYSTEM_PROMPT = """
You are GrowthMate, a merchant's sales agent. You can search the catalog,
create payment links, check order status, and report growth insights.
Only call create_payment_link once the customer has clearly confirmed the
specific product and quantity. Never invent product data — always use
search_catalog results. If a tool call is blocked, explain the block plainly
and suggest a smaller purchase if relevant. Always answer in plain English.
"""


def _make_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-3.6-flash",
        google_api_key=os.getenv("GEMINI_API_KEY"),
    ).bind_tools(TOOLS)


def _ai_content(msg) -> str:
    return getattr(msg, "content", "") or ""


def _plain_text(msg) -> str:
    """Extract the plain-text piece of a message's content (content may be a
    list of content blocks with a 'text' key, e.g. for Gemini)."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def agent_node(state: AgentState) -> AgentState:
    """
    Calls the LLM with state['messages'] + tool schemas. Appends the actual
    AIMessage (preserving its tool_calls) so Gemini sees a valid
    assistant -> functionResponse pairing on the next turn. If the response
    requests a tool, sets pending_tool_call.
    """
    llm = _make_llm()
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + list(state["messages"])
    try:
        ai_msg = llm.invoke(messages)
    except Exception:  # noqa: BLE001 - degrade to a conversational reply
        ai_msg = AIMessage(
            content="I'm sorry, I'm having trouble reaching my reasoning service right now. Please try again in a moment."
        )

    appended = list(state["messages"]) + [ai_msg]

    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    if tool_calls:
        tc = tool_calls[0]
        pending = {
            "name": tc["name"],
            "args": tc.get("args", {}),
            "id": tc.get("id") or f"call_{len(state['messages'])}",
        }
        return {
            **state,
            "messages": appended,
            "pending_tool_call": pending,
            "tools_called": state.get("tools_called", []) + [tc["name"]],
        }

    return {**state, "messages": appended, "pending_tool_call": None}


def route_after_agent(state: AgentState) -> str:
    """Conditional edge: 'end' | 'guardrail' | 'tool' based on pending call."""
    pending = state.get("pending_tool_call")
    if not pending:
        return "end"
    if pending["name"] == "create_payment_link":
        return "guardrail"
    return "tool"


def _product_price(sku: str) -> float | None:
    """§11.5: single read-only DB lookup of product price. No writes."""
    db = db_module.SessionLocal()
    try:
        product = db.query(Product).filter(Product.sku == sku).first()
        return float(product.price) if product else None
    finally:
        db.close()


def guardrail_node(state: AgentState) -> AgentState:
    """
    §11.5: for create_payment_link, does a read-only price lookup, computes
    computed_amount = price * quantity, stores it, then runs
    check_transaction. Writes last_decision / last_decision_reason.
    """
    pending = state.get("pending_tool_call") or {}
    args = pending.get("args", {})
    sku = args.get("sku")
    quantity = args.get("quantity", 1)

    price = _product_price(sku)
    if price is None:
        return {
            **state,
            "computed_amount": None,
            "last_decision": "BLOCK",
            "last_decision_reason": f"product with sku '{sku}' not found",
        }

    amount = round(price * int(quantity), 2)
    decision = guardrail_module.check_transaction(
        state.get("actor", ""), amount, state.get("spend_so_far", 0.0)
    )

    return {
        **state,
        "computed_amount": amount,
        "last_decision": "ALLOW" if decision.allowed else "BLOCK",
        "last_decision_reason": decision.reason,
    }


def tool_node(state: AgentState) -> AgentState:
    """
    Executes the actual tool function from tools.py, stores the JSON result in
    tool_result, and appends a tool-role message. The pending tool call is kept
    so audit_node can log it; audit_node clears it before handing control back
    to agent_node.
    """
    from app import tools

    pending = state.get("pending_tool_call") or {}
    name = pending.get("name")
    args = pending.get("args", {})
    tool_id = pending.get("id", "")

    try:
        if name == "search_catalog":
            result = tools.search_catalog(
                query=args.get("query", ""),
                max_price=args.get("max_price"),
            )
        elif name == "create_payment_link":
            result = tools.create_payment_link(
                sku=args.get("sku"),
                quantity=args.get("quantity", 1),
                actor=state.get("actor", ""),
                session_id=state.get("session_id", ""),
                computed_amount=state.get("computed_amount"),
            )
        elif name == "get_order_status":
            result = tools.get_order_status(order_id=args.get("order_id"))
        elif name == "get_growth_insights":
            result = tools.get_growth_insights()
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:  # noqa: BLE001 - tool must never crash the graph
        result = {"error": str(e)}

    tool_message = {
        "role": "tool",
        "tool_call_id": tool_id,
        "content": json.dumps(result),
        "name": name,
    }

    return {
        **state,
        "messages": state["messages"] + [tool_message],
        "tool_result": result,
    }


def audit_node(state: AgentState) -> AgentState:
    """
    Writes exactly one AuditLog row. Always runs (ALLOW/success and BLOCK
    paths). After logging, injects a system message so agent_node can compose
    its final reply (success text or structured refusal).
    """
    import json as _json

    pending = state.get("pending_tool_call") or {}
    name = pending.get("name", "")
    args = pending.get("args", {})
    decision = state.get("last_decision") or "N/A"
    reason = state.get("last_decision_reason")
    result = state.get("tool_result") or {}

    # Outcome determination.
    if decision == "BLOCK":
        outcome = "blocked"
    elif "error" in result:
        outcome = "failed"
    else:
        outcome = "success"

    error_detail = str(result.get("error")) if outcome == "failed" else None

    db = db_module.SessionLocal()
    try:
        log = AuditLog(
            session_id=state.get("session_id", ""),
            actor=state.get("actor", ""),
            tool_name=name,
            parameters_json=_json.dumps(args),
            agent_reasoning=None,
            guardrail_decision=decision,
            guardrail_reason=reason,
            outcome=outcome,
            error_detail=error_detail,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()

    # Inject the decision/result back into the conversation for the final agent_node.
    if decision == "BLOCK":
        injected = (
            f"The requested payment was blocked by our safety guardrails: {reason}. "
            "Please explain this plainly to the customer and suggest a smaller purchase if relevant."
        )
    else:
        injected = json.dumps(result)

    return {
        **state,
        "messages": state["messages"] + [{"role": "system", "content": injected}],
        "pending_tool_call": None,
    }


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("agent", agent_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("tool", tool_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "agent")

    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"end": END, "guardrail": "guardrail", "tool": "tool"},
    )
    graph.add_conditional_edges(
        "guardrail",
        lambda s: s["last_decision"],
        {"ALLOW": "tool", "BLOCK": "audit"},
    )
    graph.add_edge("tool", "audit")
    graph.add_edge("audit", "agent")

    return graph.compile()


graph = build_graph()
