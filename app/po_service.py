"""Purchase order use cases: create, submit, receive, detail (SPEC.md §3.1, §3.2, §5.2).

Functions work inside the caller's session. They flush but never commit: the
request's ``get_db`` dependency commits, or rolls back if a DomainError is raised.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.errors import BusinessRuleViolation, Duplicate, NotFound, detail
from app.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    POLineItem,
    POStatus,
    PurchaseOrder,
    ZERO_QTY,
    Vendor,
)
from app.schemas import GRCreate, POCreate
from app.state_machine import ensure_status, record_created, transition

# ---------------------------------------------------------------------------
# Shared lookups (also used by invoice_service)
# ---------------------------------------------------------------------------


def get_active_vendor(db: Session, vendor_id: str, *, action: str) -> Vendor:
    """R3: the vendor must exist (404) and be active (422)."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise NotFound(f"Vendor {vendor_id} not found",
                       [detail("vendor does not exist", field="vendor_id", rule="R3")])
    if not vendor.active:
        raise BusinessRuleViolation(
            f"Vendor {vendor.code} is inactive and cannot {action}",
            [detail("vendor must be active", field="vendor_id", rule="R3")])
    return vendor


def get_po(db: Session, po_id: str) -> PurchaseOrder:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound(f"Purchase order {po_id} not found")
    return po


def lock_po(db: Session, po_id: str) -> PurchaseOrder:
    """Lock the PO row before reading quantities that a write depends on (SPEC.md §7.2).

    The lock is a real UPDATE issued as the first write. On PostgreSQL it takes the
    row lock. On SQLite it opens the write transaction, which serializes concurrent
    writers. pysqlite runs SELECTs outside a transaction, so SELECT … FOR UPDATE
    would not protect anything there. Without this lock, two concurrent receipts
    could both pass the over-receipt check.
    """
    db.execute(update(PurchaseOrder).where(PurchaseOrder.id == po_id)
               .values(updated_at=datetime.now(timezone.utc))
               .execution_options(synchronize_session=False))
    return get_po(db, po_id)


def get_po_detail(db: Session, po_id: str) -> PurchaseOrder:
    """PO with vendor, lines, receipts and GR lines loaded eagerly (no N+1)."""
    po = db.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == po_id)
        .options(
            joinedload(PurchaseOrder.vendor),
            selectinload(PurchaseOrder.lines),
            selectinload(PurchaseOrder.goods_receipts).selectinload(GoodsReceipt.lines),
        )
    )
    if po is None:
        raise NotFound(f"Purchase order {po_id} not found")
    return po


# ---------------------------------------------------------------------------
# Create (DRAFT)
# ---------------------------------------------------------------------------


def _duplicate_po(po_number: str) -> Duplicate:
    return Duplicate(f"Purchase order {po_number} already exists",
                     [detail("po_number must be unique", field="po_number", rule="R1")])


def create_po(db: Session, data: POCreate, actor: str) -> PurchaseOrder:
    vendor = get_active_vendor(db, str(data.vendor_id), action="receive new purchase orders")

    seen: dict[int, int] = {}
    errors = []
    for i, line in enumerate(data.lines):
        if line.line_number in seen:
            errors.append(detail(f"line_number {line.line_number} already used by lines[{seen[line.line_number]}]",
                                 field=f"lines[{i}].line_number", rule="R2"))
        seen.setdefault(line.line_number, i)
    if errors:
        raise BusinessRuleViolation("Purchase order failed validation", errors)

    if db.scalar(select(PurchaseOrder.id).where(PurchaseOrder.po_number == data.po_number)):
        raise _duplicate_po(data.po_number)

    po = PurchaseOrder(po_number=data.po_number, vendor=vendor, order_date=data.order_date,
                       status=POStatus.DRAFT)
    po.lines = [POLineItem(line_number=l.line_number, sku=l.sku, description=l.description,
                           quantity_ordered=l.quantity_ordered, unit_price=l.unit_price,
                           quantity_received=ZERO_QTY)
                for l in data.lines]
    db.add(po)
    try:
        db.flush()
    except IntegrityError as e:  # lost a race on the unique po_number; get_db rolls back
        raise _duplicate_po(data.po_number) from e
    record_created(db, po, actor)
    return po


# ---------------------------------------------------------------------------
# Submit (DRAFT → SUBMITTED)
# ---------------------------------------------------------------------------


def submit_po(db: Session, po_id: str, actor: str) -> PurchaseOrder:
    po = lock_po(db, po_id)
    ensure_status(po, [POStatus.DRAFT], "submit purchase order")
    # The vendor may have been deactivated since the draft was created.
    get_active_vendor(db, po.vendor_id, action="receive new purchase orders")
    if not po.lines:
        raise BusinessRuleViolation(f"Purchase order {po.po_number} has no lines",
                                    [detail("at least one line is required to submit", field="lines")])
    transition(db, po, POStatus.SUBMITTED, actor)
    db.flush()
    return po


# ---------------------------------------------------------------------------
# Receive (goods receipt; partial receipts allowed)
# ---------------------------------------------------------------------------


def receive_po(db: Session, po_id: str, data: GRCreate, actor: str) -> GoodsReceipt:
    po = lock_po(db, po_id)
    ensure_status(po, [POStatus.SUBMITTED], "receive goods")  # RECEIVED means nothing is left

    errors = []
    if data.received_date < po.order_date:  # R11
        errors.append(detail(f"must be on or after the PO order date {po.order_date}",
                             field="received_date", rule="R11"))

    by_id = {pl.id: pl for pl in po.lines}
    seen: set[str] = set()
    receipts: list[tuple[POLineItem, GoodsReceiptLine]] = []
    for i, line in enumerate(data.lines):
        ref = str(line.po_line_id)
        pl = by_id.get(ref)
        if pl is None:  # R7
            errors.append(detail(f"PO line {ref} does not belong to {po.po_number}",
                                 field=f"lines[{i}].po_line_id", rule="R7"))
            continue
        if ref in seen:  # R7
            errors.append(detail(f"PO line {pl.line_number} appears more than once on this receipt",
                                 field=f"lines[{i}].po_line_id", rule="R7"))
            continue
        seen.add(ref)

        remaining = pl.quantity_ordered - pl.quantity_received
        if line.quantity_received > remaining:  # R6: can't receive more than ordered
            errors.append(detail(
                f"PO line {pl.line_number} ({pl.sku}): receiving {line.quantity_received} would exceed "
                f"the ordered {pl.quantity_ordered} (already received {pl.quantity_received}, "
                f"remaining {remaining})",
                field=f"lines[{i}].quantity_received", rule="R6"))
            continue
        receipts.append((pl, line.quantity_received))

    if errors:
        raise BusinessRuleViolation("Goods receipt failed validation", errors)

    gr = GoodsReceipt(purchase_order=po, received_by=data.received_by,
                      received_date=data.received_date)
    for pl, qty in receipts:
        gr.lines.append(GoodsReceiptLine(po_line=pl, quantity_received=qty))
        pl.quantity_received = pl.quantity_received + qty  # stored cumulative total
    db.add(gr)
    db.flush()

    if po.fully_received:  # §3.1: SUBMITTED → RECEIVED once every line is complete
        transition(db, po, POStatus.RECEIVED, actor)
        db.flush()
    return gr
