"""
buyer_agent.py — standalone external AI-buyer simulation (secondary demo path).

Speaks to the running GrowthMate API over HTTP exactly like a real third-party
agent would. Uses only httpx/requests — it must NEVER import from app/.

ARCHITECTURE §10: retained as a SECONDARY demonstration path, not the primary
architecture. Journey B is now driven through a CART whose total exceeds the
buyer_agent per-transaction limit (₹3000), rather than a single over-priced
item — the guardrail fires on the backend-computed cart_total.

Two flows:
  Journey A (success): requests a small purchase, expects the full pipeline to
      run and NOT be blocked (returns a payment link through the agent).
  Journey B (engineered failure): instructs building a cart whose total exceeds
      the buyer_agent per-transaction limit of ₹3000, confirms the checkout,
      and expects the API to return HTTP 200 with blocked=true and a structured
      refusal (a block is expected control flow, not an error).
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BUYER_BASE_URL", "http://127.0.0.1:8000")
SESSION_ID = os.getenv("BUYER_SESSION_ID", "sess-buyer-demo")
ACTOR = "buyer_agent"


def fetch_catalog() -> list[dict]:
    resp = requests.get(f"{BASE_URL}/catalog", timeout=30)
    resp.raise_for_status()
    return resp.json()["products"]


def send_chat(message: str, history: list[dict] | None = None) -> dict:
    # Optional history lets the buyer drive a multi-turn conversational
    # pipeline across separate /chat calls (LLD §3 "optional history").
    payload = {
        "session_id": SESSION_ID,
        "actor": ACTOR,
        "message": message,
    }
    if history:
        payload["history"] = history
    resp = requests.post(f"{BASE_URL}/chat", json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sample_cheap_item(products: list[dict], max_price: float) -> dict | None:
    affordable = [p for p in products if p["price"] * 1 <= max_price]
    if not affordable:
        return None
    return min(affordable, key=lambda p: p["price"])


def main() -> None:
    print(f"buyer_agent connecting to {BASE_URL} (session {SESSION_ID}, actor={ACTOR})")
    history: list[dict] = []

    products = fetch_catalog()
    print(f"catalog: {len(products)} products")

    # --- Journey A: normal purchase within limits ---
    max_budget = 2500.0
    item = sample_cheap_item(products, max_budget)
    if item:
        msg_a = (
            f"I want to buy the {item['name']} (sku {item['sku']}) at ₹{item['price']}. "
            f"I have a budget of ₹{max_budget}. Please help me complete a purchase."
        )
        print(f"Journey A -> requesting {item['sku']} (budget ₹{max_budget})")
        result_a = send_chat(msg_a)
        print("Journey A reply:", result_a.get("reply"))
        print("Journey A blocked:", result_a.get("blocked"))
        if result_a.get("tool_calls_made"):
            print("Journey A tool_calls:", result_a["tool_calls_made"])
    else:
        print("Journey A: no item within budget — skipped.")

    # --- Journey B: engineered failure (cart total exceeds per-transaction limit) ---
    # Cotton Crew T-Shirt (APP-001) costs ₹499; a cart of 7 of them = ₹3493,
    # which exceeds the buyer_agent per-transaction limit of ₹3000 — but each
    # individual item is well within a normal single-item purchase. The block
    # must fire on the backend-computed cart_total, not on any one item price.
    build_msg = (
        "I'd like to check out. Please add 7 Cotton Crew T-Shirts "
        "(sku APP-001, ₹499 each) to my cart and show me the checkout preview."
    )
    confirm_msg = "Yes, proceed with the checkout."

    print("Journey B -> building cart of 7x APP-001 (₹3493, exceeds ₹3000 limit)")

    build_resp = send_chat(build_msg, history=history)
    history = _append_history(history, build_msg, build_resp)
    print("Journey B (build) reply:", build_resp.get("reply"))
    print("Journey B (build) blocked:", build_resp.get("blocked"))

    confirm_resp = send_chat(confirm_msg, history=history)
    history = _append_history(history, confirm_msg, confirm_resp)
    print("Journey B (confirm) reply:", confirm_resp.get("reply"))
    print("Journey B (confirm) blocked:", confirm_resp.get("blocked"))

    # The demo's "bar": a block must come back as a clean 200 response.
    # The guardrail fires at execute_payment (after approval), so the final
    # response to the confirmation should signal blocked=true.
    if confirm_resp.get("blocked") is True or build_resp.get("blocked") is True:
        print("SUCCESS: engineered failure handled gracefully (HTTP 200, blocked=true)")
    else:
        print(
            "WARNING: Journey B did not return blocked=true — check guardrail "
            "config or that the cart was built over the limit.",
        )
        sys.exit(2)


def _append_history(history: list[dict], user_msg: str, resp: dict) -> list[dict]:
    """Record the user turn + assistant reply so the next /chat sees context."""
    new = list(history)
    new.append({"role": "user", "content": user_msg})
    new.append({"role": "assistant", "content": resp.get("reply", "")})
    return new


if __name__ == "__main__":
    main()
