"""
LangGraph orchestration for GrowthMate — Revision 2.

Implements ARCHITECTURE.md §6 and LOW_LEVEL_DESIGN.md §5/§6.

Five-node graph (approval_node added; NOT merged with the guardrail):
    START -> agent
    agent --(no tool call: final reply OR clarifying question)--> END
    agent --(read-only / cart / discovery tool)--> tool
    agent --(execute_payment)--> approval
    approval --(approved)--> guardrail
    approval --(not approved / unclear)--> agent
    guardrail --(ALLOW)--> tool
    guardrail --(BLOCK)--> audit
    tool --> audit
    audit --> agent

- The clarification "self-loop" is handled on agent_node: when requirements are
  incomplete, agent_node returns WITHOUT a tool call, ending its turn with a
  clarifying question. No separate node exists for it (ARCHITECTURE §6).
- `approval_node` and `guardrail_node` are two separate, sequential,
  deterministic checks; each writes its own AuditLog row.
- `validate_approval` is called directly by approval_node and is NOT an
  LLM-callable tool (LLD §6.1).
"""

import json
import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from typing import TypedDict

from app import db as db_module
from app import guardrail as guardrail_module
from app.models import AuditLog
from app.tools import TOOLS


class AgentState(TypedDict):
    messages: list
    actor: str
    session_id: str

    structured_requirements: dict | None
    requirements_complete: bool

    discovery_results: list | None
    selected_product: dict | None
    upsell_candidates: list | None

    cart: list
    cart_total: float | None
    checkout_preview: dict | None
    approval_confirmed: bool

    spend_so_far: float
    computed_amount: float | None
    last_decision: str | None            # "ALLOW" | "BLOCK" | None
    last_decision_reason: str | None

    pending_tool_call: dict | None
    payment_state: dict | None
    order_id: int | None
    tools_called: list


SYSTEM_PROMPT = """
You are GrowthMate, a merchant's AI sales agent that performs live product
discovery, recommends the best matches, upsells from the merchant's own
catalog, and gates checkout behind explicit user approval.

Use this conversational pipeline, in order:

1. REQUIREMENT GATHERING. If the user has NOT given you enough to search
   usefully (category, and ideally budget / brand / key features), ask ONE
   clarifying question at a time and DO NOT call any tool. End your turn with
   the question. Only proceed once you have enough.

2. DISCOVERY. Once requirements are clear, call
   discover_and_recommend_products with structured_requirements containing
   'budget' (a number) and 'required_features'/'keywords' (lists of strings).
   Present the TOP 3 results, each with its price and the 'why' reason.
   Do NOT present more than 3. Do NOT invent products not returned by the tool.

3. SELECTION. Wait for the user to pick one of the 3. Set the selected product
   in context for the next step.

4. UPSELL. After the user selects, call recommend_complementary_products with
   the selected product (including its category) to get 2-3 complementary
   items from our merchant catalog. Offer them to the user as add-ons.

5. CART. Use update_cart (action 'add', item with type 'external' or
   'merchant', ref_id, name, price, quantity, source) for each item the user
   accepts. Let the user adjust quantities or remove items.

6. CHECKOUT PREVIEW. When the user is ready to pay, call prepare_checkout.
   Present the preview (items, subtotal, total, currency) back to the user.

7. EXPLICIT APPROVAL. After showing the preview, ask this EXACT question:
   "Shall I proceed with the checkout?" Do NOT call execute_payment until the
   user has explicitly confirmed. Only after they confirm, request
   execute_payment. If they decline or are unsure, do not proceed.

Rules:
- The cart total is always computed by the backend — never fabricate totals.
- execute_payment is money-moving and goes through approval + guardrail checks
  you do not control. If a payment is blocked, explain the block plainly and
  suggest lowering the quantity or removing an item if relevant.
- Never invent product data; always use tool results. Answer in plain English.
"""


def _make_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        google_api_key=os.getenv("GEMINI_API_KEY"),
    ).bind_tools(TOOLS)


def _to_langchain_messages(raw_messages: list) -> list:
    """Convert the stored plain-dict history into LangChain message objects.

    `main.py` builds ``state["messages"]`` as plain dicts (``{"role": ...,
    "content": ...}``). LangChain's Gemini integration hangs or returns empty
    content when handed bare dicts or malformed tool-call/tool-result pairs, so
    we normalise here.

    The internal agent tool-use "round trip" — an ``ai`` message that carries a
    tool call (its ``content`` is empty) immediately followed by a ``tool``
    result message — is deliberately DROPPED before talking to the LLM. The same
    result is already surfaced to the model via the human-readable system message
    that ``audit_node`` injects right after a tool runs (see ``audit_node``), so
    removing the raw pair avoids feeding Gemini an invalid tool-use exchange that
    makes it reply with empty content. Only user/assistant text and those injected
    system summaries reach the LLM.
    """
    converted: list = []
    for m in raw_messages:
        # Already a LangChain message object (AIMessage from agent_node).
        if hasattr(m, "content") and not isinstance(m, dict):
            converted.append(m)
            continue
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "tool":
            converted.append(ToolMessage(content=content, tool_call_id=m.get("tool_call_id", "")))
            continue
        if role == "system":
            converted.append(SystemMessage(content=content))
        elif role in ("user", "human"):
            converted.append(HumanMessage(content=content))
        elif role in ("assistant", "ai"):
            if content:  # only include non-empty assistant text from history
                converted.append(AIMessage(content=content))
    return converted


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


# Read-only discovery-style tools: running one of these more than once per user
# turn adds nothing and is the classic source of an infinite agent->tool loop
# (this caused the `GraphRecursionError` / "no reply" symptom). Re-invoking any
# of these consecutively is treated as a loop and forces a plain-text reply.
# State-changing tools (update_cart, execute_payment, etc.) are NOT guarded so
# legitimate sequences like two sequential cart adds still work.
_LOOP_GUARD_TOOLS = {"discover_and_recommend_products", "recommend_complementary_products"}


def agent_node(state: AgentState) -> AgentState:
    """
    Reasons over the conversation. Returns END (no tool call: final reply or a
    clarifying question when requirements are incomplete), routes to approval
    for execute_payment, or to tool for any other tool.
    """
    llm = _make_llm()
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + _to_langchain_messages(
        state["messages"]
    )
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
        name = tc["name"]
        # Break a discover-loop / any same-tool loop: if the agent just executed
        # this exact tool as its most recent action (no new user input since), a
        # re-request adds no value and would spin in an infinite agent->tool loop
        # (this is what caused `GraphRecursionError` and the "no reply" symptom).
        # End the turn so the agent composes a reply from the result it already
        # has. Legitimate multi-tool sequences (discover -> upsell -> update_cart)
        # use different names and are unaffected.
        if name in _LOOP_GUARD_TOOLS and state.get("tools_called") and state["tools_called"][-1] == name:
            # Force a text reply from the LLM (without tools) so the user gets
            # a real response instead of empty content.
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
                text_llm = ChatGoogleGenerativeAI(
                    model="gemini-3.5-flash-lite",
                    google_api_key=os.getenv("GEMINI_API_KEY"),
                )
                ai_text = text_llm.invoke(_to_langchain_messages(
                    [SystemMessage(content="You just received a tool result. "
                     "Compose a clear, helpful reply to the user based on the "
                     "latest information. Do NOT call any tools — just reply in "
                     "plain text.")] + list(state["messages"])
                ))
                ai_text = AIMessage(content=_plain_text(ai_text) or "Here's what I found. Let me know if you'd like to proceed!")
            except Exception:  # noqa: BLE001
                ai_text = AIMessage(content="Here's what I found based on the search. Let me know if you'd like to proceed!")
            return {**state, "messages": list(state["messages"]) + [ai_text], "pending_tool_call": None}

        pending = {
            "name": name,
            "args": tc.get("args", {}),
            "id": tc.get("id") or f"call_{len(state['messages'])}",
        }
        return {
            **state,
            "messages": appended,
            "pending_tool_call": pending,
            "tools_called": state.get("tools_called", []) + [name],
        }

    # No tool call: either a clarifying question (requirements incomplete) or a
    # final reply. Either way we end the turn and wait for the next user message.
    return {**state, "messages": appended, "pending_tool_call": None}


def route_after_agent(state: AgentState) -> str:
    """start -> 'end' | 'approval' | 'tool' based on the pending tool call."""
    pending = state.get("pending_tool_call")
    if not pending:
        return "end"
    if pending["name"] == "execute_payment":
        return "approval"
    return "tool"


# ---------------------------------------------------------------------------
# approval_node — separate, deterministic check from guardrail (LLD §6.1).
# ---------------------------------------------------------------------------

def approval_node(state: AgentState) -> AgentState:
    """
    Deterministically validates whether the user explicitly approved this
    specific checkout (checkout_preview was shown AND the most recent user
    message is affirmative). Writes its own AuditLog row, then routes to
    guardrail (approved) or back to agent (not approved / unclear).

    `validate_approval` is callable directly here — never an LLM tool.
    """
    approved = guardrail_module.validate_approval(state)

    db = db_module.SessionLocal()
    try:
        db.add(
            AuditLog(
                session_id=state.get("session_id", ""),
                actor=state.get("actor", ""),
                event_type="approval",
                tool_name="validate_approval",
                parameters_json=json.dumps({"pending": state.get("pending_tool_call")}),
                agent_reasoning=None,
                decision="ALLOW" if approved else "BLOCK",
                reason="user explicitly approved checkout" if approved
                else "user did not explicitly approve, or no checkout preview shown",
                outcome="approved" if approved else "blocked",
                error_detail=None,
            )
        )
        db.commit()
    finally:
        db.close()

    next_state = {
        **state,
        "approval_confirmed": True if approved else False,
    }

    if not approved:
        # Inject a system message so agent_node composes a re-prompt.
        injected = (
            "The user has not explicitly confirmed this checkout. "
            "Do NOT call execute_payment. Present the checkout preview clearly "
            "and ask: 'Shall I proceed with the checkout?' again, or help them "
            "adjust their cart."
        )
        return {
            **next_state,
            "messages": state["messages"] + [{"role": "system", "content": injected}],
            "pending_tool_call": None,
        }

    # Approved — leave the pending execute_payment for guardrail/tool.
    return {**next_state, "pending_tool_call": state.get("pending_tool_call")}


def route_after_approval(state: AgentState) -> str:
    return "guardrail" if state.get("approval_confirmed") else "agent"


# ---------------------------------------------------------------------------
# guardrail_node — spend-limit enforcement against cart_total (LLD §6.2).
# ---------------------------------------------------------------------------

def guardrail_node(state: AgentState) -> AgentState:
    """
    Checks the payment amount (cart_total) against per-transaction /
    per-session limits. Only reached AFTER approval_node passes. Sets
    computed_amount / last_decision / last_decision_reason.
    """
    cart_total = state.get("cart_total")
    if cart_total is None:
        return {
            **state,
            "computed_amount": None,
            "last_decision": "BLOCK",
            "last_decision_reason": "cart total is not available for the guardrail check",
        }

    amount = round(float(cart_total), 2)
    decision = guardrail_module.check_transaction(
        state.get("actor", ""), amount, state.get("spend_so_far", 0.0)
    )

    return {
        **state,
        "computed_amount": amount,
        "last_decision": "ALLOW" if decision.allowed else "BLOCK",
        "last_decision_reason": decision.reason,
    }


def _coerce(value):
    """Gemini often hands structured args as JSON *strings*. Parse any string
    that looks like JSON back into a dict/list so downstream tools get real
    objects (this is what caused `update_cart` to write nothing: `dict(...)`
    on a JSON string raises, the tool returned an error, and the cart stayed
    empty)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001 - not JSON; return as-is
            return value
    return value


def tool_node(state: AgentState) -> AgentState:
    """Executes whichever tool was requested; never crashes the graph."""
    from app import tools

    pending = state.get("pending_tool_call") or {}
    name = pending.get("name")
    args = {k: _coerce(v) for k, v in (pending.get("args", {}) or {}).items()}
    tool_id = pending.get("id", "")

    try:
        if name == "discover_and_recommend_products":
            result = tools.discover_and_recommend_products(
                structured_requirements=args.get("structured_requirements") or {}
            )
        elif name == "recommend_complementary_products":
            result = tools.recommend_complementary_products(
                selected_product=args.get("selected_product") or {}
            )
        elif name == "update_cart":
            item = dict(args.get("item") or {})
            item["session_id"] = state.get("session_id", "")
            item["actor"] = state.get("actor", "")
            result = tools.update_cart(
                action=args.get("action", ""),
                item=item,
            )
        elif name == "prepare_checkout":
            result = tools.prepare_checkout(session_id=state.get("session_id", ""))
        elif name == "execute_payment":
            result = tools.execute_payment(
                session_id=state.get("session_id", ""),
                actor=state.get("actor", ""),
            )
        elif name == "get_payment_status":
            result = tools.get_payment_status(order_id=args.get("order_id"))
        elif name == "get_growth_insights":
            result = tools.get_growth_insights()
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:  # noqa: BLE001 - tool must never crash the graph
        result = {"error": str(e)}

    # Carry forward confirmed items / cart total for payment tool.
    if name == "prepare_checkout":
        cart_total = result.get("total")
    elif name == "execute_payment":
        cart_total = state.get("cart_total")
    else:
        cart_total = state.get("cart_total")

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
        "checkout_preview": result if name == "prepare_checkout" else state.get("checkout_preview"),
        "cart_total": cart_total if name in ("prepare_checkout", "execute_payment") else state.get("cart_total"),
    }


def _event_type_for(name: str, decision: str | None) -> str:
    """Map a tool call + decision to the expanded audit taxonomy (LLD §2.5)."""
    if decision == "BLOCK":
        return "guardrail_decision"
    mapping = {
        "discover_and_recommend_products": "discovery",
        "recommend_complementary_products": "upsell",
        "update_cart": "cart_update",
        "prepare_checkout": "checkout_preview",
        "execute_payment": "payment_attempt",
        "get_payment_status": "payment_result",
        "get_growth_insights": "requirement_extraction",
    }
    return mapping.get(name, "failure" if decision is None else "tool")


def _format_tool_result(name: str, result: dict) -> str:
    """Render a tool result as clear, readable text the agent reliably acts on.

    The raw JSON dump was too opaque: the agent would misread discovery output
    and reply "nothing available" even when in-budget matches existed. Each
    result type is rendered explicitly so the agent presents real data.
    """
    if name == "discover_and_recommend_products":
        recs = result.get("recommendations") or []
        lines = [f"Discovered {result.get('count', len(recs))} matching product(s):"]
        for i, r in enumerate(recs, 1):
            lines.append(
                f"{i}. {r.get('name')} — ₹{r.get('price')} ({r.get('currency')}) "
                f"[{r.get('source')}] — {r.get('why')}"
            )
        if not recs:
            lines.append("No products matched the requirements.")
        return "\n".join(lines)
    if name == "recommend_complementary_products":
        recs = result.get("recommendations") or result.get("candidates") or []
        lines = [f"Complementary add-ons ({len(recs)}):"]
        for r in recs:
            lines.append(
                f"- {r.get('name')} — ₹{r.get('price')} ({r.get('currency')}) "
                f"[{r.get('source')}]"
            )
        if not recs:
            lines.append("No complementary products found.")
        return "\n".join(lines)
    # Default: compact JSON, but the agent should still get the full payload.
    import json as _json

    return _json.dumps(result)


def audit_node(state: AgentState) -> AgentState:
    """
    Writes exactly one AuditLog row per tool pass, for every stage. Always runs
    on the ALLOW/success path and the BLOCK path. After logging, injects the
    tool result (readable text) so agent_node can compose its final reply.
    """
    pending = state.get("pending_tool_call") or {}
    name = pending.get("name", "")
    args = pending.get("args", {})
    decision = state.get("last_decision")
    reason = state.get("last_decision_reason")
    result = state.get("tool_result") or {}

    if decision == "BLOCK":
        outcome = "blocked"
    elif "error" in result:
        outcome = "failed"
    else:
        outcome = "success"

    error_detail = str(result.get("error")) if outcome == "failed" else None
    event_type = _event_type_for(name, decision)
    if decision == "BLOCK":
        event_type = "guardrail_decision"

    db = db_module.SessionLocal()
    try:
        log = AuditLog(
            session_id=state.get("session_id", ""),
            actor=state.get("actor", ""),
            event_type=event_type,
            tool_name=name,
            parameters_json=json.dumps(args),
            agent_reasoning=None,
            decision=decision,
            reason=reason,
            outcome=outcome,
            error_detail=error_detail,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()

    if decision == "BLOCK":
        injected = (
            f"The requested payment was blocked by our safety guardrails: {reason}. "
            "Please explain this plainly to the customer and suggest lowering "
            "the quantity or removing an item if relevant."
        )
        return {
            **state,
            "messages": state["messages"] + [{"role": "user", "content": injected}],
            "pending_tool_call": None,
        }
    # Non-BLOCK: the tool result is already delivered to the agent via the
    # ToolMessage in state["messages"] — no injection needed.
    return {**state, "pending_tool_call": None}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("agent", agent_node)
    graph.add_node("approval", approval_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("tool", tool_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "agent")

    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"end": END, "approval": "approval", "tool": "tool"},
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"guardrail": "guardrail", "agent": "agent"},
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
