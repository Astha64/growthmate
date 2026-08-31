# AGENTS.md — GrowthMate

## What this project is
AI Growth & Agentic Commerce hackathon submission. A merchant agent on Razorpay
test-mode APIs, transactable by both a human (chat) and an external AI buyer agent,
with every money-moving action gated by a deterministic guardrail and logged to an
audit trail. Deadline-driven — prefer working over elegant.

## Required reading before any code change
Read these in full before writing or modifying anything. They are the binding spec,
not background reading:
- `docs/ARCHITECTURE.md` — integrations, agent orchestration shape
- `docs/HIGH_LEVEL_DESIGN.md` — module boundaries, data flow, NFRs
- `docs/LOW_LEVEL_DESIGN.md` — exact schemas, API contracts, tool JSON schemas,
  guardrail rules, LangGraph node signatures, sequence diagrams
- `docs/BUILD_PROMPT.md` — full build order and per-file responsibilities

Column names, endpoint paths, tool names, and LangGraph node names must match
`LOW_LEVEL_DESIGN.md` exactly — do not rename or "improve" them independently.

## Non-negotiable rules
- Guardrail checks (`app/guardrail.py`) are plain deterministic Python. Never let
  the LLM decide whether a payment executes.
- Every tool call — allowed, blocked, succeeded, or failed — writes exactly one
  `AuditLog` row. No exceptions to this.
- Agent orchestration is a LangGraph `StateGraph` with the four nodes from
  `LOW_LEVEL_DESIGN.md` §6 (`agent_node`, `guardrail_node`, `tool_node`,
  `audit_node`) — do not flatten this into one function.
- `buyer_agent.py` is a standalone script (uses `httpx`/`requests` only) that talks
  to the running API over HTTP like a real external party — it must never import
  from `app/`.
- No secrets in source. Only `.env` (see `.env.example`), never committed.
- Every external call (LLM, Razorpay) is wrapped so failures become a normal
  conversational reply — no unhandled exception may reach the client. See the
  error-handling matrix in `LOW_LEVEL_DESIGN.md` §9.

## Stack
Python 3.11, FastAPI, SQLAlchemy + SQLite, Anthropic Claude (tool-use) via
LangChain binding, LangGraph for orchestration, Razorpay Python SDK (test mode),
vanilla HTML/CSS/JS frontend, pytest + httpx for tests.

## Commands
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python seed_data.py
uvicorn app.main:app --reload
pytest
```

## Structure
```
app/            FastAPI routes, models, guardrail, tools, agent graph
frontend/       chat + audit viewer, static HTML/JS
tests/          pytest suite
buyer_agent.py  standalone external-agent simulation (repo root, not in app/)
docs/           architecture / HLD / LLD / build prompt — source of truth
```

## Gotchas
- `app/main.py` is routing only — no business logic. If you're writing DB queries
  or LLM calls there, they belong in another module per the file responsibility
  map in `LOW_LEVEL_DESIGN.md` §10.
- Guardrail tests (`tests/test_guardrail.py`) must not touch the DB or network —
  they test pure functions only.
- The "engineered failure" demo (buyer agent exceeding its per-transaction limit)
  must return HTTP 200 with `"blocked": true`, not a 4xx/5xx — a block is expected
  control flow, not an error.
