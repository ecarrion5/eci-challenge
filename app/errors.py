"""Domain errors and the error envelope from SPEC.md §4.7.

Services raise ``DomainError`` subclasses. The handlers registered by
``register_exception_handlers`` turn them, and FastAPI's own request-validation
errors, into ``{"error": {"code", "message", "details"}}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError

from app.schemas import ErrorBody, ErrorCode, ErrorDetail, ErrorResponse


def detail(message: str, *, field: str | None = None, rule: str | None = None) -> ErrorDetail:
    return ErrorDetail(field=field, rule=rule, message=message)


class DomainError(Exception):
    code: ErrorCode
    status_code: int

    def __init__(self, message: str, details: list[ErrorDetail] | None = None,
                 data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        self.data = data


class NotFound(DomainError):
    code, status_code = ErrorCode.NOT_FOUND, 404


class BusinessRuleViolation(DomainError):
    code, status_code = ErrorCode.BUSINESS_RULE_VIOLATION, 422


class Duplicate(DomainError):
    code, status_code = ErrorCode.DUPLICATE, 409


class InvalidTransition(DomainError):
    code, status_code = ErrorCode.INVALID_TRANSITION, 409


class VersionConflict(DomainError):
    code, status_code = ErrorCode.VERSION_CONFLICT, 409


class PoVendorMismatch(DomainError):
    """The invoice's PO belongs to a different vendor (400)."""

    code, status_code = ErrorCode.PO_VENDOR_MISMATCH, 400


class AmountExceedsReceived(DomainError):
    """Match failed: the invoice amount is more than the value received on the PO (422)."""

    code, status_code = ErrorCode.AMOUNT_EXCEEDS_RECEIVED, 422


class InstructionNotUnderstood(DomainError):
    """The agent could not turn the instruction into an API call (422)."""

    code, status_code = ErrorCode.INSTRUCTION_NOT_UNDERSTOOD, 422


def _envelope(status_code: int, code: ErrorCode, message: str, details: list[ErrorDetail],
              data: dict[str, Any] | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details, data=data))
    return JSONResponse(status_code=status_code,
                        content=body.model_dump(mode="json", exclude={"error": {"data"}} if data is None else None))


def _field_path(loc: tuple[Any, ...]) -> str:
    # ("body", "lines", 0, "quantity") -> "lines[0].quantity"
    parts = list(loc[1:] if loc and loc[0] == "body" else loc)
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "body"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_req: Request, exc: DomainError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message, exc.details, exc.data)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        details = [detail(e["msg"], field=_field_path(tuple(e["loc"]))) for e in exc.errors()]
        return _envelope(422, ErrorCode.VALIDATION_ERROR, "Request validation failed", details)

    @app.exception_handler(StaleDataError)
    async def _stale(_req: Request, _exc: StaleDataError) -> JSONResponse:
        return _envelope(409, ErrorCode.VERSION_CONFLICT, "The resource was modified concurrently", [])
