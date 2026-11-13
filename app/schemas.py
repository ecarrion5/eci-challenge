"""Pydantic v2 request/response schemas (SPEC.md §4, field rules §5.1).

Conventions:
- Request models reject unknown fields (F7) and trim strings (F1).
- Decimals are serialized to JSON as strings, e.g. "25.00" (§1.2). This is
  Pydantic v2's default for ``Decimal``.
- Response models are built from ORM objects (``from_attributes=True``).
- Cross-entity business rules (§5.2) belong to the service layer, not here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Generic, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.models import (
    EntityType,
    InvoiceStatus,
    PaymentTerms,
    POStatus,
    ReceiptStatus,
    VendorCategory,
)

# ---------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------

MAX_LINES = 500  # F6


def _not_future(d: date) -> date:  # F5
    if d > datetime.now(timezone.utc).date():
        raise ValueError("date must not be in the future")
    return d


def _str(max_length: int, *, min_length: int = 1, pattern: str | None = None):
    return Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=min_length, max_length=max_length, pattern=pattern
        ),
    ]


def _scale(places: int):
    quantum = Decimal(1).scaleb(-places)
    # Exact: the decimal_places constraint has already rejected anything finer. This
    # pads "750" to "750.00" so responses look the same before and after a DB round trip.
    return AfterValidator(lambda v: v.quantize(quantum))


Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2), _scale(2)]  # F3
Quantity = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=4), _scale(4)]  # F2
NotFutureDate = Annotated[date, AfterValidator(_not_future)]  # F5
Reason = _str(500, min_length=3)  # F8

VendorCode = _str(32, pattern=r"^[A-Z0-9_-]+$")
Name = _str(255)
Sku = _str(64)
Description = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=255)]
DocNumber32 = _str(32)
InvoiceNumber = _str(64)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)  # F7, F1


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageParams(BaseModel):
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


# ---------------------------------------------------------------------------
# Vendor (§2.2, §4.2)
# ---------------------------------------------------------------------------


class VendorCreate(RequestModel):
    code: VendorCode
    name: Name
    payment_terms: PaymentTerms  # F4
    category: VendorCategory  # F4
    active: bool = True


class VendorUpdate(RequestModel):
    name: Name | None = None
    payment_terms: PaymentTerms | None = None
    category: VendorCategory | None = None
    active: bool | None = None


class VendorRead(ResponseModel):
    id: UUID
    code: str
    name: str
    payment_terms: PaymentTerms
    category: VendorCategory
    active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# PurchaseOrder / POLineItem (§2.3, §2.4, §4.3)
# ---------------------------------------------------------------------------


class POLineCreate(RequestModel):
    line_number: int = Field(ge=1)
    sku: Sku
    description: Description = None
    quantity_ordered: Quantity
    unit_price: Money


class POCreate(RequestModel):
    po_number: DocNumber32
    vendor_id: UUID
    order_date: NotFutureDate
    lines: list[POLineCreate] = Field(min_length=1, max_length=MAX_LINES)


class POUpdate(RequestModel):
    """DRAFT only. ``lines``, when given, replaces every line."""

    order_date: NotFutureDate | None = None
    lines: list[POLineCreate] | None = Field(None, max_length=MAX_LINES)
    version: int = Field(ge=1)  # R13


# ---------------------------------------------------------------------------
# GoodsReceipt (§2.5, §4.4)
# ---------------------------------------------------------------------------


class GRLineCreate(RequestModel):
    po_line_id: UUID
    quantity_received: Quantity


class GRCreate(RequestModel):
    received_by: _str(64)
    received_date: NotFutureDate
    lines: list[GRLineCreate] = Field(min_length=1, max_length=MAX_LINES)


class GRLineRead(ResponseModel):
    id: UUID
    po_line_id: UUID
    quantity_received: Decimal


class GRRead(ResponseModel):
    id: UUID
    po_id: UUID
    received_by: str
    received_date: date
    lines: list[GRLineRead]


# ---------------------------------------------------------------------------
# PO detail (GET /purchase-orders/{id} and the other PO endpoints' responses)
# ---------------------------------------------------------------------------


class VendorSummary(ResponseModel):
    id: UUID
    code: str
    name: str
    payment_terms: PaymentTerms
    category: VendorCategory
    active: bool


class POLineDetail(ResponseModel):
    id: UUID
    line_number: int
    sku: str
    description: str | None
    unit_price: Decimal
    quantity_ordered: Decimal
    line_total: Decimal
    quantity_received: Decimal  # derived: Σ goods-receipt lines
    quantity_remaining: Decimal  # derived: ordered − received, never negative
    receipt_status: ReceiptStatus  # derived


class PODetail(ResponseModel):
    id: UUID
    po_number: str
    vendor_id: UUID
    vendor: VendorSummary
    order_date: date
    status: POStatus
    receipt_status: ReceiptStatus  # derived from the lines
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[POLineDetail]
    goods_receipts: list[GRRead]


class ReceiveResponse(BaseModel):
    """Response of ``POST /purchase-orders/{id}/receive``."""

    goods_receipt: GRRead
    purchase_order: PODetail


# ---------------------------------------------------------------------------
# Invoice and 3-way match
# ---------------------------------------------------------------------------


class InvoiceCreate(RequestModel):
    vendor_id: UUID
    po_id: UUID
    invoice_number: InvoiceNumber
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2), _scale(2)]
    # Optional: defaults to today (UTC). Drives due_date through the vendor's payment terms.
    invoice_date: date | None = None


class InvoiceRead(ResponseModel):
    id: UUID
    invoice_number: str
    vendor_id: UUID
    po_id: UUID
    invoice_date: date
    due_date: date  # derived from the vendor's payment terms
    status: InvoiceStatus
    amount: Decimal


class MatchLine(BaseModel):
    po_line_id: UUID
    line_number: int
    sku: str
    quantity_ordered: Decimal
    quantity_received: Decimal
    unit_price: Decimal
    received_value: Decimal  # quantity_received × unit_price


class MatchResponse(BaseModel):
    """Successful match: the invoice amount is covered by the value received."""

    invoice_id: UUID
    invoice_status: InvoiceStatus
    invoice_amount: Decimal
    received_value: Decimal
    # True while any line still has quantity_received < quantity_ordered. This is
    # informational: it plays no part in deciding whether the match passes.
    partial_receipt_pending: bool
    lines: list[MatchLine]



class ReasonRequest(RequestModel):
    """Body of ``POST /purchase-orders/{id}/close``."""

    reason: Reason


# ---------------------------------------------------------------------------
# GLEntry (§2.7) and StatusHistory (§2.9)
# ---------------------------------------------------------------------------


class GLEntryRead(ResponseModel):
    id: UUID
    invoice_id: UUID
    debit: Decimal
    credit: Decimal
    posted_at: datetime


class StatusHistoryRead(ResponseModel):
    id: UUID
    entity_type: EntityType
    entity_id: UUID
    from_status: str | None
    to_status: str
    actor: str
    reason: str | None
    at: datetime


# ---------------------------------------------------------------------------
# Errors (§4.7) and health
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    NOT_FOUND = "NOT_FOUND"
    DUPLICATE = "DUPLICATE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PO_VENDOR_MISMATCH = "PO_VENDOR_MISMATCH"
    AMOUNT_EXCEEDS_RECEIVED = "AMOUNT_EXCEEDS_RECEIVED"
    INSTRUCTION_NOT_UNDERSTOOD = "INSTRUCTION_NOT_UNDERSTOOD"


class ErrorDetail(BaseModel):
    field: str | None = None
    rule: str | None = None
    message: str


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    # Machine-readable values for errors that carry numbers, e.g. a failed match.
    data: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Agent wrapper (POST /agent/instruct)
# ---------------------------------------------------------------------------


class AgentRequest(RequestModel):
    instruction: _str(1000)


class AgentApiCall(BaseModel):
    method: str
    path: str
    body: Any | None = None


class AgentApiResponse(BaseModel):
    status_code: int
    body: Any


class AgentResponse(BaseModel):
    instruction: str
    intent: str
    parameters: dict[str, Any]  # as extracted from the instruction, before resolving to ids
    api_call: AgentApiCall
    api_response: AgentApiResponse  # exactly what the API returned, including errors
    summary: str  # one line, plain English
