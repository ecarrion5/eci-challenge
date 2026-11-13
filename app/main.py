"""FastAPI entrypoint. Run with: uvicorn app.main:app --reload"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.database import init_db
from app.deps import DbSession
from app.errors import register_exception_handlers
from app.routers import agent, invoices, purchase_orders
from app.schemas import HealthResponse


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()  # dev convenience; replace with Alembic migrations later
    yield


app = FastAPI(title="PO / Invoice 3-Way Match Agent", version="0.1.0", lifespan=lifespan)
register_exception_handlers(app)
app.include_router(purchase_orders.router)
app.include_router(invoices.router)
app.include_router(agent.router)


@app.get("/api/v1/health", response_model=HealthResponse, tags=["health"])
def health(db: DbSession) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ok")
