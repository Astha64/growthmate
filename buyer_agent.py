"""
buyer_agent.py — standalone external AI-buyer simulation.

Speaks to the running GrowthMate API over HTTP exactly like a real third-party
agent would. Uses only httpx/requests — it must NEVER import from app/.

Two flows:
  Journey A (success): reads /catalog, purchases an item within its limits,
      receives a Razorpay payment link.
  Journey B (engineered failure): requests APP-001 x10 = INR 4990, which
      exceeds the buyer_agent per-transaction limit of INR 3000 -> the API
      returns HTTP 200 with blocked=true and a structured refusal (LLD §11.5).
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


def send_chat(message: str) -> dict:
    # Since the LLM decides tool calls, the buyer agent keeps it simple: it
    # passes a clear instruction and trusts the API to orchestrate. For the
    # deterministic Journey B demo we send a precise directive.
    payload = {
        "session_id": SESSION_ID,
        "actor": ACTOR,
        "message": message,
    }
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

    products = fetch_catalog()
    print(f"catalog: {len(products)} products")

    # --- Journey A: normal purchase within limits ---
    max_budget = 2500.0
    item = sample_cheap_item(products, max_budget)
    if item:
        msg = (
            f"I want to buy the {item['name']} (sku {item['sku']}) at ₹{item['price']}. "
            f"Please create the payment link for 1 unit."
        )
        print(f"Journey A -> requesting {item['sku']} (budget ₹{max_budget})")
        result_a = send_chat(msg)
        print("Journey A reply:", result_a.get("reply"))
        print("Journey A blocked:", result_a.get("blocked"))
        if result_a.get("tool_calls_made"):
            print("Journey A tool_calls:", result_a["tool_calls_made"])
    else:
        print("Journey A: no item within budget — skipped.")

    # --- Journey B: engineered failure (exceeds per-transaction limit) ---
    # APP-001 costs ₹499; quantity 10 => ₹4990 > buyer_agent limit of ₹3000.
    msg_b = (
        "I need to buy the Cotton Crew T-Shirt (sku APP-001). "
        "Please create a payment link for a quantity of 10."
    )
    print("Journey B -> requesting APP-001 x10 (₹4990, exceeds ₹3000 limit)")
    result_b = send_chat(msg_b)
    print("Journey B reply:", result_b.get("reply"))
    print("Journey B blocked:", result_b.get("blocked"))
    if result_b.get("tool_calls_made"):
        print("Journey B tool_calls:", result_b["tool_calls_made"])

    # The demo's "bar": a block must come back as a clean 200 response, not a crash.
    if result_b.get("blocked") is True:
        print("SUCCESS: engineered failure handled gracefully (HTTP 200, blocked=true)")
    else:
        print("WARNING: Journey B did not return blocked=true — check guardrail config")
        sys.exit(2)


if __name__ == "__main__":
    main()
