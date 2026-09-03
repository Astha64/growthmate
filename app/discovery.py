"""
Live Product Discovery — deterministic internal pipeline (Revision 2).

Implements LOW_LEVEL_DESIGN.md §4.1 and §7, and ARCHITECTURE.md §7/§10.

The discovery pipeline (search -> extract -> normalize -> dedupe -> filter ->
rank) is ONE deterministic internal pipeline inside a single LLM-facing tool
call (`discover_and_recommend_products`). The sub-stages are plain internal
functions — they are NOT separate LLM-callable tools.

The external product sources are represented by a small, deterministic mock
catalog so the pipeline runs offline during the hackathon. Each source is
exposed via `search_external_sources`, which degrades gracefully: a failing
or empty source is skipped rather than failing the whole request
(ARCHITECTURE §7 — sources deliver a list of raw candidate dicts).

rank_by_fit scores on four named, explainable factors (LLD §7):
  1. requirement-keyword match
  2. budget closeness (how close the price is to the budget, not just <=)
  3. feature match
  4. availability
Each factor contributes to a `_score` and a `_why` string so the
recommendation text stays explainable.
"""

import os

from dataclasses import dataclass, field

import requests

# ---------------------------------------------------------------------------
# Internal mock "external" product sources.
#
# The structure mirrors what a real search/product-data provider would return:
# a list of raw candidate dicts (possibly heterogeneous, possibly with missing
# keys). Tests may monkeypatch `search_external_sources` to test the pipeline
# against arbitrary candidate sets.
# ---------------------------------------------------------------------------

_MOCK_SOURCES: dict[str, list[dict]] = {
    "marketplace_api": [
        {
            "source_url": "https://market.test/products/1",
            "title": "Nike Air Zoom Pegasus 40",
            "price": 2100,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "road", "cushioned"],
            "rating": 4.5,
            "availability": "in_stock",
        },
        {
            "source_url": "https://market.test/products/2",
            "title": "Adidas Ultraboost Light",
            "price": 3600,
            "currency": "INR",
            "brand": "Adidas",
            "features": ["running", "cushioned"],
            "rating": 4.7,
            "availability": "in_stock",
        },
        {
            "source_url": "https://market.test/products/3",
            "title": "Asics Gel-Kayano 30",
            "price": 2450,
            "currency": "INR",
            "brand": "Asics",
            "features": ["running", "stability"],
            "rating": 4.4,
            "availability": "in_stock",
        },
    ],
    "price_comparison_api": [
        {
            "source_url": "https://compare.test/p/1",
            "title": "Nike Air Zoom Pegasus 40",
            "price": 2050,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "road"],
            "rating": 4.6,
            "availability": "in_stock",
        },
        {
            "source_url": "https://compare.test/p/2",
            "title": "Puma Velocity Nitro 2",
            "price": 1900,
            "currency": "INR",
            "brand": "Puma",
            "features": ["running", "road"],
            "rating": 4.2,
            "availability": "in_stock",
        },
        {
            "source_url": "https://compare.test/p/3",
            "title": "New Balance Fresh Foam 880",
            "price": 2700,
            "currency": "INR",
            "brand": "New Balance",
            "features": ["running", "daily"],
            "rating": 4.5,
            "availability": "out_of_stock",
        },
    ],
    "deals_api": [
        {
            "source_url": "https://deals.test/d/1",
            "title": "Nike Revolution 6",
            "price": 1799,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "budget"],
            "rating": 4.1,
            "availability": "in_stock",
        },
        {
            "source_url": "https://deals.test/d/2",
            "title": "Reebok Floatride Energy 4",
            "price": 2300,
            "currency": "INR",
            "brand": "Reebok",
            "features": ["running", "road"],
            "rating": 4.3,
            "availability": "in_stock",
        },
    ],
}


def _build_search_query(requirements: dict) -> str:
    """Turn structured requirements into a search phrase for a real web engine.

    Uses the explicit product category the user asked for, plus any noted
    keywords (brand, feature, type), so the query targets what the user
    explicitly requested rather than the merchant's curated DB.
    """
    category = (requirements.get("category") or "").strip()
    keywords = (requirements.get("keywords") or [])

    terms = [t for t in [category] + list(keywords) if t]
    if not terms:
        terms = ["product"]
    # Add a marketplace intent so results look like purchasable listings.
    return " ".join(terms) + " buy"


def _serpapi_shopping(query: str, api_key: str) -> list[dict]:
    """Query SerpAPI Google Shopping and return raw product candidate dicts."""
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": api_key,
        "hl": "en",
        "gl": "in",
        "num": 10,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("shopping_results") or []
    candidates: list[dict] = []
    for item in results:
        price = 0.0
        price_str = item.get("price") or ""
        try:
            price = float(str(price_str).replace("₹", "").replace(",", "").strip())
        except ValueError:
            price = 0.0
        candidates.append({
            "source_url": item.get("link") or item.get("product_link") or "",
            "title": item.get("title") or item.get("name") or "",
            "price": price,
            "currency": "INR",
            "brand": item.get("brand") or "",
            "features": [],
            "rating": float(item.get("rating") or 0.0),
            "availability": "in_stock" if item.get("in_stock") else "unknown",
        })
    return candidates


def search_external_sources(requirements: dict) -> list[dict]:
    """Return raw candidate dicts from real external product sources.

    Uses SerpAPI (Google Shopping) with `SERPAPI_KEY` from the environment to
    search actual websites for the product the user explicitly asked for. If
    `SERPAPI_KEY` is not configured or the live call fails/returns nothing, this
    degrades gracefully to the deterministic offline mock catalog so the system
    still works end-to-end during the hackathon and in tests.

    The final list is the concatenation of raw candidate dicts across sources.
    """
    query = _build_search_query(requirements)
    api_key = os.getenv("SERPAPI_KEY")

    candidates: list[dict] = []
    if api_key:
        try:
            live = _serpapi_shopping(query, api_key)
            candidates.extend(live)
        except Exception:  # noqa: BLE001 - a failing live source never fails discovery
            candidates = []

    # Fallback to deterministic mock catalog when no live data is available.
    if not candidates:
        for source_name, raw_items in _MOCK_SOURCES.items():
            try:
                candidates.extend(raw_items)
            except Exception:  # noqa: BLE001 - skip a failing source, never fail discovery
                continue
    return candidates


# ---------------------------------------------------------------------------
# Pipeline stages (internal functions, not LLM tools — LLD §7).
# ---------------------------------------------------------------------------

def extract_product_data(raw: dict) -> dict:
    """Extract a normal internal dict from a heterogeneous raw candidate.

    Handles missing/renamed keys gracefully, pulling only the fields
    `normalize_listing` and downstream stages rely on.
    """
    return {
        "source_url": raw.get("source_url") or raw.get("url") or "",
        "title": raw.get("title") or raw.get("name") or "",
        "price": float(raw.get("price") or raw.get("amount") or 0.0),
        "currency": raw.get("currency") or "INR",
        "brand": raw.get("brand") or "",
        "features": raw.get("features") or raw.get("tags") or [],
        "rating": float(raw.get("rating") or 0.0),
        "availability": raw.get("availability") or "unknown",
    }


def normalize_listing(extracted: dict) -> dict:
    """Normalize into a single canonical shape used downstream.

    Also derives a stable dedup key (lowercased title + brand) so
    `deduplicate_listings` can group near-identical listings offered by
    multiple sources. `scroll_id` is assigned during dedup.
    """
    title = (extracted.get("title") or "").strip().lower()
    brand = (extracted.get("brand") or "").strip().lower()
    return {
        "source_url": extracted.get("source_url", ""),
        "title": extracted.get("title", ""),
        "price": extracted.get("price", 0.0),
        "currency": extracted.get("currency", "INR"),
        "brand": extracted.get("brand", ""),
        "features": list(extracted.get("features") or []),
        "rating": extracted.get("rating", 0.0),
        "availability": extracted.get("availability", "unknown"),
        "_dedup_key": f"{title}|{brand}",
        "_dedup_group_id": None,
    }


def deduplicate_listings(normalized: list[dict]) -> list[dict]:
    """Group listings on the same dedup key; keep the best-scoring member.

    Group members share a `_dedup_group_id`; within a group we keep the listing
    with the best (rating, then lower price) and drop the rest, per LLD §2.2
    ("the pipeline keeps the best-scored member of each group and discards the
    rest before ranking").
    """
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for listing in normalized:
        groups[listing["_dedup_key"]].append(listing)

    deduped: list[dict] = []
    for group_id, (key, members) in enumerate(groups.items()):
        members.sort(key=lambda m: (-m.get("rating", 0.0), m.get("price", 0.0)))
        winner = members[0]
        winner = dict(winner)
        winner["_dedup_group_id"] = key  # a stable group identifier (LLD §2.2)
        deduped.append(winner)
    return deduped


def meets_hard_constraints(product: dict, requirements: dict) -> bool:
    """Deterministic hard filtering: reject anything outside mandatory bounds.

    - Budget: if requirements['budget'] set, product.price must be <= budget.
    - Required features: every keyword in requirements['required_features']
      must appear as a substring match against the product's name/features.
    - Availability: reject 'out_of_stock' when requirements want stock.
    Fail-closed: any unrecognised/missing requirement field is ignored, but a
    defined rule that fails excludes the product.
    """
    budget = requirements.get("budget")
    if budget is not None and product.get("price", 0.0) > float(budget):
        return False

    req_features = requirements.get("required_features") or []
    haystack = " ".join(
        [product.get("title", "").lower()]
        + [f.lower() for f in product.get("features", [])]
    )
    for feature in req_features:
        # Split multi-word features (e.g. "running shoes") into individual
        # tokens. A product matches if ANY of the tokens appear in the
        # haystack — this way "running shoes" matches products tagged "running".
        tokens = str(feature).lower().split()
        if not tokens:
            continue
        if not any(tok in haystack for tok in tokens):
            return False

    availability = requirements.get("requires_availability", True)
    if availability and product.get("availability") == "out_of_stock":
        return False

    return True


def rank_by_fit(products: list[dict], requirements: dict) -> list[dict]:
    """Rank products by an explainable multi-factor score, best first.

    Factors (each aggregated into the `_why` text for explainability):
      1. keyword_bonus   — how many requirement keywords appear in title/features
      2. budget_closeness— closeness of price to budget: 1.0 if within, gently
                           decreasing as price rises toward the budget ceiling
      3. feature_bonus   — fraction of required features matched
      4. availability_bonus — prefer in-stock items
    """
    budget = requirements.get("budget")
    req_keywords = [
        str(k).lower()
        for k in (requirements.get("required_features") or [])
    ] + [
        str(k).lower()
        for k in (requirements.get("keywords") or [])
    ]

    scored: list[tuple[float, dict]] = []
    for product in products:
        title = product.get("title", "").lower()
        features = [f.lower() for f in product.get("features", [])]
        haystack = " ".join([title] + features)

        keyword_bonus = sum(1 for kw in req_keywords if kw in haystack) / max(1, len(req_keywords or [1]))

        price = product.get("price", 0.0)
        if budget is not None and budget > 0:
            closeness = max(0.0, 1.0 - abs(price - float(budget)) / float(budget))
        else:
            closeness = 1.0

        req_ft = requirements.get("required_features") or []
        feature_bonus = sum(1 for f in req_ft if str(f).lower() in haystack) / max(1, len(req_ft or [1]))

        availability_bonus = 1.0 if product.get("availability") not in ("out_of_stock", "unknown") else 0.0

        score = (0.4 * keyword_bonus) + (0.3 * closeness) + (0.2 * feature_bonus) + (0.1 * availability_bonus)

        why_parts = []
        if keyword_bonus > 0:
            why_parts.append("matches your keywords")
        if product.get("price", 0) <= (budget or float("inf")):
            why_parts.append(f"within your ₹{budget} budget")
        if feature_bonus > 0:
            why_parts.append("covers your required features")
        if product.get("availability") == "out_of_stock":
            why_parts.append("currently out of stock")
        why = ", ".join(why_parts) if why_parts else "best fit among what's available"

        product["_score"] = round(score, 3)
        product["_why"] = why
        scored.append((score, product))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored]


# ---------------------------------------------------------------------------
# Public pipeline entry point — the ONLY LLM-facing discovery tool.
# ---------------------------------------------------------------------------

def discover_and_recommend_products(structured_requirements: dict) -> dict:
    """Run the full deterministic discovery pipeline and return top 3.

    Implements LLD §7 exactly:
        search -> extract -> normalize -> dedupe -> filter -> rank -> top 3
    Returns {"count": N, "recommendations": [{name, price, source, why}, ...]}.
    """
    candidates = search_external_sources(structured_requirements)
    extracted = [extract_product_data(c) for c in candidates]
    normalized = [normalize_listing(e) for e in extracted]
    deduped = deduplicate_listings(normalized)
    filtered = [p for p in deduped if meets_hard_constraints(p, structured_requirements)]
    ranked = rank_by_fit(filtered, structured_requirements)

    recommendations = []
    for p in ranked[:3]:
        recommendations.append(
            {
                "name": p.get("title", ""),
                "price": p.get("price", 0.0),
                "currency": p.get("currency", "INR"),
                "source": p.get("source_url", ""),
                "why": p.get("_why", ""),
            }
        )

    return {
        "count": len(recommendations),
        "recommendations": recommendations,
    }
