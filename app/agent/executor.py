"""Resolve a parsed instruction into an API call, and summarize the API's answer.

``plan_call`` resolves the references in the instruction with short read-only
lookups: vendor code → id, PO number → id, SKU → PO line, invoice number → id.
It then builds the exact request for one of our endpoints. Business rules are
NOT re-implemented here. The API decides, and the agent reports what it said.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.asgi_client import ApiResult
from app.agent.parser import Instruction, Intent, ReceiptItem
from app.database import SessionLocal
from app.errors import InstructionNotUnderstood, InvalidTransition, NotFound, detail
from app.models import Invoice, PurchaseOrder, Vendor

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class Plan:
    method: str
    path: str  # relative to /api/v1
    body: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)  # names used in the summary


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------


def _vendor(db: Session, ref: str) -> Vendor:
    vendor = db.get(Vendor, ref) if _UUID.match(ref) else (
        db.scalar(select(Vendor).where(func.lower(Vendor.code) == ref.lower()))
        or db.scalar(select(Vendor).where(func.lower(Vendor.name) == ref.lower())))
    if vendor is None:
        known = ", ".join(db.scalars(select(Vendor.code).order_by(Vendor.code)))
        raise NotFound(f"Vendor '{ref}' not found", [detail(f"known vendor codes: {known}", field="vendor")])
    return vendor


def _po(db: Session, ref: str) -> PurchaseOrder:
    if _UUID.match(ref):
        po = db.get(PurchaseOrder, ref)
    else:
        po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == ref))
        if po is None:
            hits = db.scalars(select(PurchaseOrder)
                              .where(func.lower(PurchaseOrder.po_number) == ref.lower())).all()
            po = hits[0] if len(hits) == 1 else None
    if po is None:
        raise NotFound(f"Purchase order '{ref}' not found", [detail("unknown PO number or id", field="po")])
    return po


def _invoice(db: Session, ref: str, vendor_ref: str | None) -> Invoice:
    if _UUID.match(ref):
        invoice = db.get(Invoice, ref)
        if invoice is None:
            raise NotFound(f"Invoice '{ref}' not found")
        return invoice
    q = select(Invoice).where(func.lower(Invoice.invoice_number) == ref.lower())
    if vendor_ref:
        q = q.where(Invoice.vendor_id == _vendor(db, vendor_ref).id)
    hits = db.scalars(q).all()
    if not hits:
        raise NotFound(f"Invoice '{ref}' not found" + (f" for vendor {vendor_ref}" if vendor_ref else ""))
    if len(hits) > 1:
        vendors = ", ".join(sorted(i.vendor.code for i in hits))
        raise InstructionNotUnderstood(
            f"Several vendors have an invoice numbered '{ref}' ({vendors})",
            [detail("say which vendor, e.g. 'Match invoice INV-77 from ACME'", field="vendor")])
    return hits[0]


def _receipt_lines(po: PurchaseOrder, items: list[ReceiptItem] | None, receive_all: bool) -> list[dict]:
    if receive_all:
        lines = [{"po_line_id": pl.id, "quantity_received": str(pl.quantity_remaining)}
                 for pl in po.lines if pl.quantity_remaining > 0]
        if not lines:
            raise InvalidTransition(f"Nothing left to receive on {po.po_number}: it is {po.status}, "
                                    f"{po.receipt_status}")
        return lines

    out = []
    for item in items or []:
        if item.line_number is not None:
            matches = [pl for pl in po.lines if pl.line_number == item.line_number]
            what = f"line {item.line_number}"
        else:
            matches = [pl for pl in po.lines if pl.sku.lower() == (item.sku or "").lower()]
            what = f"SKU {item.sku}"
        if not matches:
            skus = ", ".join(f"line {pl.line_number} {pl.sku}" for pl in po.lines)
            raise InstructionNotUnderstood(f"{po.po_number} has no {what}",
                                           [detail(f"lines on this PO: {skus}", field="items")])
        if len(matches) > 1:
            nums = ", ".join(str(pl.line_number) for pl in matches)
            raise InstructionNotUnderstood(f"{what} is on several lines of {po.po_number} ({nums})",
                                           [detail("say e.g. '4 of line 1' instead", field="items")])
        out.append({"po_line_id": matches[0].id, "quantity_received": str(item.quantity)})
    return out


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_call(instruction: Instruction, actor: str) -> Plan:
    """Build the API request for an instruction (sync: runs the DB lookups)."""
    p = instruction.params
    with SessionLocal() as db:
        match instruction.intent:
            case Intent.CREATE_PO:
                vendor = _vendor(db, p["vendor"])
                po_number = p.get("po_number") or (
                    f"PO-AG-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}")
                return Plan("POST", "/purchase-orders", {
                    "po_number": po_number, "vendor_id": vendor.id,
                    "order_date": p.get("order_date") or _today(),
                    "lines": [{"line_number": n, "sku": it.sku, "quantity_ordered": str(it.quantity),
                               "unit_price": str(it.unit_price)} for n, it in enumerate(p["items"], 1)],
                })
            case Intent.SUBMIT_PO:
                po = _po(db, p["po"])
                return Plan("POST", f"/purchase-orders/{po.id}/submit", None, {"po_number": po.po_number})
            case Intent.GET_PO:
                po = _po(db, p["po"])
                return Plan("GET", f"/purchase-orders/{po.id}", None, {"po_number": po.po_number})
            case Intent.RECEIVE_GOODS:
                po = _po(db, p["po"])
                return Plan("POST", f"/purchase-orders/{po.id}/receive", {
                    "received_by": p.get("received_by") or actor,
                    "received_date": p.get("received_date") or _today(),
                    "lines": _receipt_lines(po, p.get("items"), bool(p.get("receive_all"))),
                }, {"po_number": po.po_number})
            case Intent.CREATE_INVOICE:
                # The vendor is resolved on its own, never taken from the PO, so the
                # API's vendor/PO check (400 PO_VENDOR_MISMATCH) still applies.
                vendor, po = _vendor(db, p["vendor"]), _po(db, p["po"])
                body = {"vendor_id": vendor.id, "po_id": po.id,
                        "invoice_number": p["invoice_number"], "amount": str(p["amount"])}
                if p.get("invoice_date"):
                    body["invoice_date"] = p["invoice_date"]
                return Plan("POST", "/invoices", body,
                            {"po_number": po.po_number, "vendor_code": vendor.code})
            case Intent.MATCH_INVOICE:
                invoice = _invoice(db, p["invoice"], p.get("vendor"))
                return Plan("POST", f"/invoices/{invoice.id}/match", None, {
                    "invoice_number": invoice.invoice_number,
                    "po_number": invoice.purchase_order.po_number})
    raise AssertionError(f"unhandled intent {instruction.intent}")


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def _q(value: Any) -> str:
    """Quantity for display: '10.0000' → '10', '2.5000' → '2.5'."""
    return f"{Decimal(str(value)).normalize():f}"


_FAILED = {
    Intent.CREATE_PO: "Could not create the PO",
    Intent.SUBMIT_PO: "Could not submit {po_number}",
    Intent.GET_PO: "Could not load {po_number}",
    Intent.RECEIVE_GOODS: "Could not record the receipt on {po_number}",
    Intent.CREATE_INVOICE: "Could not record the invoice",
    Intent.MATCH_INVOICE: "Could not match invoice {invoice_number}",
}


def summarize(intent: Intent, plan: Plan, result: ApiResult) -> str:
    b, ctx = result.body, plan.context
    if not 200 <= result.status_code < 300:
        err = b.get("error", {}) if isinstance(b, dict) else {}
        data = err.get("data") or {}
        if err.get("code") == "AMOUNT_EXCEEDS_RECEIVED":
            return (f"Invoice {ctx['invoice_number']} was not matched: its amount {data['invoice_amount']} "
                    f"exceeds the {data['received_value']} received on {ctx['po_number']} by "
                    f"{data['excess']}, so it stays {data['invoice_status']}.")
        first = (err.get("details") or [{}])[0].get("message")
        reason = err.get("message", "unexpected response") + (f" ({first})" if first else "")
        return f"{_FAILED[intent].format(**ctx)}: {reason} [{result.status_code} {err.get('code', '')}]."

    match intent:
        case Intent.CREATE_PO:
            items = ", ".join(f"{_q(l['quantity_ordered'])} × {l['sku']} at {l['unit_price']}" for l in b["lines"])
            total = sum((Decimal(l["line_total"]) for l in b["lines"]), Decimal("0"))
            return (f"Created draft PO {b['po_number']} for {b['vendor']['code']}: {items} "
                    f"(total {total}); submit it to send it for fulfillment.")
        case Intent.SUBMIT_PO:
            return f"Submitted {b['po_number']} for fulfillment; it is now {b['status']}."
        case Intent.GET_PO:
            progress = ", ".join(f"{_q(l['quantity_received'])}/{_q(l['quantity_ordered'])} {l['sku']}"
                                 for l in b["lines"])
            return (f"{b['po_number']} for {b['vendor']['code']} is {b['status']} "
                    f"({b['receipt_status'].replace('_', ' ').lower()}): {progress} received.")
        case Intent.RECEIVE_GOODS:
            po = b["purchase_order"]
            sku = {l["id"]: l["sku"] for l in po["lines"]}
            got = ", ".join(f"{_q(l['quantity_received'])} × {sku[l['po_line_id']]}"
                            for l in b["goods_receipt"]["lines"])
            left = [f"{_q(l['quantity_remaining'])} × {l['sku']}" for l in po["lines"]
                    if Decimal(l["quantity_remaining"]) > 0]
            tail = (f"still outstanding: {', '.join(left)}" if left
                    else f"everything is received and the PO is now {po['status']}")
            return f"Received {got} on {po['po_number']}; {tail}."
        case Intent.CREATE_INVOICE:
            return (f"Recorded invoice {b['invoice_number']} from {ctx['vendor_code']} for {b['amount']} "
                    f"against {ctx['po_number']}; it is {b['status']} and due {b['due_date']}.")
        case Intent.MATCH_INVOICE:
            tail = "; some goods are still outstanding" if b["partial_receipt_pending"] else ""
            return (f"Invoice {ctx['invoice_number']} is {b['invoice_status']}: its amount {b['invoice_amount']} "
                    f"is covered by the {b['received_value']} received on {ctx['po_number']}{tail}.")
    raise AssertionError(f"unhandled intent {intent}")
