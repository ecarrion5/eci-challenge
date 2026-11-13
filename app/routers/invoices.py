"""Invoice endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app import invoice_service
from app.deps import Actor, DbSession
from app.schemas import ErrorResponse, InvoiceCreate, InvoiceRead, MatchLine, MatchResponse

router = APIRouter(prefix="/api/v1/invoices", tags=["invoices"])


def _errors(*codes: int) -> dict:
    return {code: {"model": ErrorResponse} for code in codes}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvoiceRead,
             responses=_errors(400, 404, 409, 422), summary="Register a vendor invoice against a PO")
def create_invoice(body: InvoiceCreate, db: DbSession, actor: Actor) -> InvoiceRead:
    """Creates the invoice as PENDING.

    - 400 `PO_VENDOR_MISMATCH`: `po_id` does not belong to `vendor_id`.
    - 404: the vendor or PO doesn't exist.
    - 409: the PO is not SUBMITTED or RECEIVED, or the invoice number is already used for this vendor.
    - 422: `vendor_id` is missing, the vendor is inactive, or a field is invalid.
    """
    invoice = invoice_service.create_invoice(db, body, actor)
    return InvoiceRead.model_validate(invoice)


@router.post("/{invoice_id}/match", response_model=MatchResponse,
             responses=_errors(404, 409, 422), summary="3-way match: invoice amount vs value received")
def match_invoice(invoice_id: UUID, db: DbSession, actor: Actor) -> MatchResponse:
    """Compares the invoice amount with Σ(quantity_received × unit_price) over the PO's lines.

    - Pass: the invoice becomes MATCHED (200). `partial_receipt_pending` is true when
      any line is still short of its ordered quantity. It is informational only.
    - Fail: 422 `AMOUNT_EXCEEDS_RECEIVED` with the numbers in `error.data`; the
      invoice stays PENDING.
    """
    r = invoice_service.match_invoice(db, str(invoice_id), actor)
    return MatchResponse(
        invoice_id=r.invoice.id,
        invoice_status=r.invoice.status,
        invoice_amount=r.invoice.amount,
        received_value=r.received_value,
        partial_receipt_pending=r.partial_receipt_pending,
        lines=[MatchLine(po_line_id=lv.line.id, line_number=lv.line.line_number, sku=lv.line.sku,
                         quantity_ordered=lv.line.quantity_ordered,
                         quantity_received=lv.line.quantity_received,
                         unit_price=lv.line.unit_price, received_value=lv.received_value)
                for lv in r.lines],
    )
