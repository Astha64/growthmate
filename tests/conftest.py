"""
Shared pytest fixtures.

Per LOW_LEVEL_DESIGN.md §11.8, DB-touching tests (tools, chat) use a temporary
SQLite DB by overriding app.db.SessionLocal to a test engine. Guardrail tests
use no DB at all.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as app_db
from app import models


@pytest.fixture()
def db_session_factory():
    """Yields a factory producing sessions bound to an in-memory SQLite DB.

    Uses StaticPool so every connection (including those opened inside TestClient
    request threads) shares the same single in-memory database — required because
    sqlite:///:memory: otherwise creates a fresh DB per connection.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    models.Base.metadata.create_all(bind=engine)

    original = app_db.SessionLocal
    app_db.SessionLocal = testing_session
    yield testing_session
    app_db.SessionLocal = original
    engine.dispose()
