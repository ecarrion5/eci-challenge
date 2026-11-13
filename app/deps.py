"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.database import get_db

# scope="function": get_db's commit runs after the endpoint returns but BEFORE the
# response is sent. With the default "request" scope, the client could get a 201 for
# a transaction that hasn't committed yet, or that later fails to commit.
DbSession = Annotated[Session, Depends(get_db, scope="function")]


def get_actor(x_user: Annotated[str | None, Header(max_length=64)] = None) -> str:
    """Actor for the audit history: the ``X-User`` header, or "system" (SPEC.md §1.2)."""
    actor = (x_user or "").strip()
    return actor or "system"


Actor = Annotated[str, Depends(get_actor)]
