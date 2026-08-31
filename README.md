# GrowthMate — AI Growth & Agentic Commerce Agent

A merchant-side agent on **Razorpay test-mode APIs**, transactable by both a
human (chat UI) and an autonomous external AI buyer agent (`buyer_agent.py`),
with every money-moving action gated by a **deterministic guardrail** and
logged to a queryable **audit trail**.

**The bar (problem statement):** every money action explainable, bounded, and
gated. Show the audit trail and one failure handled gracefully.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate                 # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                     # then fill in real keys (below)
python seed_data.py                      # loads the 10-item catalog
uvicorn app.main:app --reload
```

Visit http://127.0.0.1:8000/health — should return:
```json
{"status": "ok", "service": "growthmate-backend"}
```

Interactive API docs: http://127.0.0.1:8000/docs

Chat UI: http://127.0.0.1:8000/static/index.html
Audit viewer: http://127.0.0.1:8000/static/audit.html

### Environment variables (`.env`)

| Key | Purpose |
|---|---|
| `GEMINI_API_KEY` | LLM (tool-use) reasoning (Google Gemini free tier) |
| `RAZORPAY_KEY_ID` | Razorpay test-mode public key |
| `RAZORPAY_KEY_SECRET` | Razorpay test-mode secret (webhook HMAC too) |
| `DATABASE_URL` | SQLite URL (default `sqlite:///./growthmate.db`) |
| `BUYER_BASE_URL` | base URL `buyer_agent.py` talks to (default `http://127.0.0.1:8000`) |

Never commit `.env`. Secrets live only in `.env` (see `.env.example`).

---

## Architecture

Read the design docs, in order — they are the binding spec:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — integrations, agent orchestration shape
- [`docs/HIGH_LEVEL_DESIGN.md`](docs/HIGH_LEVEL_DESIGN.md) — module boundaries, data flow, NFRs
- [`docs/LOW_LEVEL_DESIGN.md`](docs/LOW_LEVEL_DESIGN.md) — schemas, API contracts, tool JSON schemas,
  guardrail rules, LangGraph node signatures, sequence diagrams (§11 addendum resolves ambiguities)
- [`docs/BUILD_PROMPT.md`](docs/BUILD_PROMPT.md) — build order and per-file responsibilities

### How it works

```
Human (chat UI) ─┐
                 ├─► POST /chat ─► LangGraph StateGraph ─► reply
buyer_agent.py ──┘
                     │  agent_node (LLM decides next tool)
                     ▼
              route by tool
              ┌─────────────┴────────────┐
              │ read-only tool           │ money tool (create_payment_link)
              ▼                          ▼
           tool_node              guardrail_node (deterministic check_transaction)
                                      │                    │
                                   ALLOW                 BLOCK
                                      ▼                    ▼
                                   tool_node ─┐        audit_node
                                      │       │           │ (refusal injected)
                                      └──► audit_node ◄───┘
                                               │
                                               ▼
                                            agent_node → final reply
```

- **Guardrail** (`app/guardrail.py`) is plain deterministic Python — the LLM never
  decides whether a payment executes.
- **Every** tool call — allowed, blocked, succeeded, or failed — writes exactly one
  `AuditLog` row.
- `buyer_agent.py` is a standalone script using `requests` only; it never imports
  from `app/`.

---

## Demo journeys

### Journey A — normal human purchase

1. Open the chat UI (http://127.0.0.1:8000/static/index.html).
2. Ask: *"I need running shoes under 2000."*
3. Confirm a product and ask it to create a payment link.
4. You receive a real Razorpay **test** payment link.
5. Check the audit viewer — the row shows `ALLOW` / `success`.

### Journey B — engineered failure (buyer agent exceeds its limit)

The buyer agent requests `APP-001` x10 = **₹4990**, which exceeds the
`buyer_agent` per-transaction limit of **₹3000**.

```bash
python buyer_agent.py
```

Expected output includes `Journey B blocked: True` and a clean structured
refusal (HTTP 200 with `"blocked": true`) — **not** a 4xx/5xx or an exception.
The audit viewer shows the `BLOCK` row with reason
`exceeds per-transaction limit of ₹3000`, and **no Order row / no Razorpay call**
was made.

---

## Tests

```bash
pytest
```

Guardrail tests are pure-function (no DB/network). Tools/chat tests use an
in-memory SQLite DB and monkeypatch the Razorpay wrapper and the LLM — no real
API keys or network required.

---

## Deployment (Render)

No `Procfile` — this project deploys on **Render** (LLD §11.10) using a
dashboard-configured start command:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Settings in the Render dashboard:
- **Runtime:** Python 3 (see `runtime.txt`)
- **Start command:** the line above
- **Environment:** set `GEMINI_API_KEY`, `RAZORPAY_KEY_ID`,
  `RAZORPAY_KEY_SECRET`, `DATABASE_URL` on the service
- A persistent disk is recommended so the SQLite file survives restarts
