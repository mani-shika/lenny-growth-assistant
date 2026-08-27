"""Shared fixtures.

Two tiers, on purpose:

* **Infrastructure-free tests** cover parsing, chunking, BM25, fusion, routing,
  the Ship 30 validator and the sanitiser. They run on a clean checkout with no
  Postgres, no Ollama and no API keys, so CI can never be "green because it
  skipped everything".
* **Database tests** use SQLite for persistence and API-contract coverage. The
  vector column and the `<=>` operator are Postgres-only, so those tests assert
  the *lexical* path and the API contract - which is exactly the part that must
  keep working when pgvector is unavailable anyway.

`TEST_DATABASE_URL` points the second tier at a real Postgres when you want the
dense path exercised too.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EMBEDDINGS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "warning")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A fresh schema per test, so ordering can never matter."""
    from app.db.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, future=True)

    if engine.dialect.name == "sqlite":
        # pgvector's Vector type has no SQLite compiler. Persistence and
        # retrieval-by-lexical do not need it, so we map it to a plain string
        # for the duration of these tests rather than skipping them entirely.
        from sqlalchemy import String
        from sqlalchemy.ext.compiler import compiles
        from pgvector.sqlalchemy import Vector

        @compiles(Vector, "sqlite")
        def _compile_vector(_type, _compiler, **_kw):  # noqa: ANN001, ANN202
            return String().compile()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def sample_transcript() -> str:
    """A miniature transcript with the exact shape of the real corpus."""
    return """---
title: "How to find product/market fit | Jane Doe"
date: "2026-03-14"
type: "podcast"
guest: "Jane Doe"
post_url: "https://www.lennysnewsletter.com/p/how-to-find-pmf"
word_count: 120
---

**Lenny Rachitsky** (00:00:12):
How do you know when you have product/market fit?

**Jane Doe** (00:00:20):
Retention is the only signal that matters. If people come back on their own,
you have it. If they need reminding, you do not.

**Lenny Rachitsky** (00:04:35):
What about pricing?

**Jane Doe** (00:04:41):
Charge earlier than feels comfortable. Willingness to pay is the fastest
proxy for value that exists.
"""


@pytest.fixture
def sample_newsletter() -> str:
    return """---
title: "A guide to growth loops"
date: "2026-04-01"
type: "newsletter"
post_url: "https://www.lennysnewsletter.com/p/growth-loops"
---

# Growth loops

A growth loop is a closed system where the output becomes the input.

## Why loops beat funnels

Funnels leak. Loops compound, because every new user creates the conditions
for the next one.
"""
