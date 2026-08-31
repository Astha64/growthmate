"""
GrowthMate backend entrypoint.

Day 1 scope: just prove the server runs and returns JSON.
Day 2 will add the /chat endpoint here.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="GrowthMate API", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    service: str


@app.get("/health", response_model=HealthResponse)
def health_check():
    """
    Simple liveness check.
    Frontend / deployment platform can ping this to confirm the backend is up.
    """
    return HealthResponse(status="ok", service="growthmate-backend")


@app.get("/")
def root():
    return {"message": "GrowthMate API is running. See /docs for API docs."}
