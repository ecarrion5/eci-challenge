"""Invoice use cases: create and 3-way match.

Functions work inside the caller's session. They flush but never commit: the
request's ``get_db`` dependency commits, or rolls back if a DomainError is raised.

Match rule: received_value = Σ (quantity_received × unit_price) over the PO's lines,
using the cumulative ``quantity_received`` stored on each POLineItem.
- invoice.amount > received_value → 422 AMOUNT_EXCEEDS_RECEIVED; the status is unchanged.
- otherwise → MATCHED. ``partial_receipt_pending`` reports whether any line is still
  short. It is informational and plays no part in the pass/fail decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import (
    AmountExceedsReceived,
    BusinessRuleViolation,
    Duplicate,
    NotFound,
    PoVendorMismatch,
    detail,
)
from app.models import Invoice, InvoiceStatus, POLineItem, POStatus, Vendor, round_money
from app.po_service import get_active_vendor, get_po, lock_po
from app.schemas import InvoiceCreate
from app.state_machine import ensure_status, record_created, transition

INVOICEABLE_PO_STATUSES = (POStatus.SUBMITTED, POStatus.RECEIVED)
MATCHABLE_INVOICE_STATUSES = (InvoiceStatus.PENDING, InvoiceStatus.MATCHED)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _duplicate(invoice_number: str, vendor_code: str) -> Duplicate:
    return Duplicate(f"Invoice {invoice_number} already exists for vendor {vendor_code}",
                     [detail("duplicate invoice number for this vendor",
                             field="invoice_number", rule="R8")])


def _ensure_unique_number(db: Session, vendor: Vendor, invoice_number: str) -> None:
    """R8: unique per vendor, ignoring case. The schema has already trimmed whitespace."""
    exists = db.scalar(
        select(Invoice.id).where(Invoice.vendor_id == vendor.id,
                                 func.lower(Invoice.invoice_number) == invoice_number.lower())
    )
    if exists:
        raise _duplicate(invoice_number, vendor.code)


def create_invoice(db: Session, data: InvoiceCreate, actor: str) -> Invoice:
    # A missing or null vendor_id is rejected by the schema (422). This check
    # covers a vendor that doesn't exist (404) or is inactive (422).
    vendor = get_active_vendor(db, str(data.vendor_id), action="submit invoices")
    po = get_po(db, str(data.po_id))

    if po.vendor_id != vendor.id:
        raise PoVendorMismatch(
            f"Purchase order {po.po_number} does not belong to vendor {vendor.code}",
            [detail(f"{po.po_number} belongs to a different vendor", field="po_id", rule="R4")],
            data={"po_id": po.id, "po_vendor_id": po.vendor_id, "vendor_id": vendor.id})
    ensure_status(po, INVOICEABLE_PO_STATUSES, "invoice this purchase order")  # R5

    invoice_date = data.invoice_date or datetime.now(timezone.utc).date()
    if invoice_date < po.order_date:  # R12
        raise BusinessRuleViolation(
            "Invoice failed validation",
            [detail(f"must be on or after the PO order date {po.order_date}",
                    field="invoice_date", rule="R12")])

    _ensure_unique_number(db, vendor, data.invoice_number)

    vendor_code = vendor.code  # read now: after a failed flush the session can't lazy-load
    invoice = Invoice(invoice_number=data.invoice_number, vendor=vendor, purchase_order=po,
                      invoice_date=invoice_date, amount=data.amount,
                      status=InvoiceStatus.PENDING)
    db.add(invoice)
    try:
        db.flush()
    except IntegrityError as e:  # lost a race on the R8 unique index; get_db rolls back
        raise _duplicate(data.invoice_number, vendor_code) from e
    record_created(db, invoice, actor)
    return invoice


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineValue:
    line: POLineItem
    received_value: Decimal


@dataclass(frozen=True)
class MatchSummary:
    invoice: Invoice
    received_value: Decimal
    partial_receipt_pending: bool
    lines: list[LineValue]


def get_invoice(db: Session, invoice_id: str) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFound(f"Invoice {invoice_id} not found")
    return invoice


def match_invoice(db: Session, invoice_id: str, actor: str) -> MatchSummary:
    invoice = get_invoice(db, invoice_id)
    ensure_status(invoice, MATCHABLE_INVOICE_STATUSES, "match invoice")
    po = lock_po(db, invoice.po_id)  # no receipt can change quantities while we read them

    lines = [LineValue(pl, pl.quantity_received * pl.unit_price) for pl in po.lines]
    received_value = round_money(sum((lv.received_value for lv in lines), Decimal("0")))
    partial_receipt_pending = any(pl.quantity_received < pl.quantity_ordered for pl in po.lines)

    if invoice.amount > received_value:
        # Raising makes get_db roll back, so the status stays as it was (PENDING).
        raise AmountExceedsReceived(
            f"Invoice amount {invoice.amount} exceeds the value received on "
            f"{po.po_number} ({received_value})",
            [detail("must not exceed the value received on the purchase order",
                    field="amount", rule="MATCH")],
            data={
                "invoice_id": invoice.id,
                "invoice_status": invoice.status,
                "invoice_amount": invoice.amount,
                "received_value": received_value,
                "excess": invoice.amount - received_value,
                "partial_receipt_pending": partial_receipt_pending,
            })

    if invoice.status != InvoiceStatus.MATCHED:
        transition(db, invoice, InvoiceStatus.MATCHED, actor)
        db.flush()
    return MatchSummary(invoice, received_value, partial_receipt_pending,
                        [LineValue(lv.line, round_money(lv.received_value)) for lv in lines])
