# GrowthMate — AI Growth & Agentic Commerce Agent

Hackathon project. An AI agent that recommends products, completes a real
Razorpay checkout via tool calling, and surfaces growth insights to the store owner.

## Status: Day 1 — skeleton only (health check works, no agent yet)

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in real keys
uvicorn app.main:app --reload
```

Visit http://127.0.0.1:8000/health — should return:
```json
{"status": "ok", "service": "growthmate-backend"}
```

Interactive API docs: http://127.0.0.1:8000/docs

## Architecture

Browser (chat UI) → FastAPI (`/chat`, `/webhook/razorpay`) → Agent loop
→ Tools (product search, create payment, growth insights) → SQLite / Razorpay API

## Roadmap
- [x] Day 1: FastAPI skeleton
- [ ] Day 2: LLM chat endpoint
- [ ] Day 3: DB + product search tool
- [ ] Day 4: Razorpay payment tool
- [ ] Day 5: Multi-tool agentic flow + growth insights
- [ ] Day 6: Frontend + tests
- [ ] Day 7: Deployment
