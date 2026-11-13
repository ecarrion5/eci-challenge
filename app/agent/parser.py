"""Turn a plain-English instruction into an intent and raw parameters.

Pure and deterministic: no I/O. The parser extracts references as written
(vendor code, PO number, SKU). The executor resolves them to ids.

Supported intents and example phrasings:
    create_po       Create a PO for 50 units of SKU-9234 from vendor ACME at $12.50/unit
    submit_po       Submit PO-2026-0001
    receive_goods   Receive 4 units of BOLT-M8 on PO-2026-0001 | Receive everything on PO-2026-0001
    get_po          Show PO-2026-0001
    create_invoice  Record invoice INV-77 from ACME for $100 on PO-2026-0001
    match_invoice   Match invoice INV-77 from ACME
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    CREATE_PO = "create_po"
    SUBMIT_PO = "submit_po"
    RECEIVE_GOODS = "receive_goods"
    GET_PO = "get_po"
    CREATE_INVOICE = "create_invoice"
    MATCH_INVOICE = "match_invoice"


EXAMPLES = [
    "Create a PO for 50 units of SKU-9234 from vendor ACME at $12.50/unit",
    "Submit PO-2026-0001",
    "Receive 4 units of BOLT-M8 on PO-2026-0001",
    "Receive everything on PO-2026-0001",
    "Show PO-2026-0001",
    "Record invoice INV-77 from ACME for $100 on PO-2026-0001",
    "Match invoice INV-77 from ACME",
]


class InstructionError(ValueError):
    """The instruction could not be parsed. ``hint`` tells the user how to fix it."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class LineItem:
    quantity: Decimal
    sku: str
    unit_price: Decimal


@dataclass(frozen=True)
class ReceiptItem:
    quantity: Decimal
    sku: str | None = None
    line_number: int | None = None


@dataclass
class Instruction:
    intent: Intent
    params: dict[str, Any] = field(default_factory=dict)

    def public_params(self) -> dict[str, Any]:
        """Parameters as JSON-friendly values, for the response."""
        def conv(v: Any) -> Any:
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, (LineItem, ReceiptItem)):
                return {k: conv(x) for k, x in v.__dict__.items() if x is not None}
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v
        return {k: conv(v) for k, v in self.params.items() if v is not None}


# ---------------------------------------------------------------------------
# Token patterns
# ---------------------------------------------------------------------------

_NUM = r"\d[\d,]*(?:\.\d+)?"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_DOC = r"[A-Za-z0-9][\w-]*\d[\w-]*"  # a document number contains at least one digit
_CODE = r"[A-Za-z0-9][\w&-]*"
_UNITS = r"(?:units?|pcs|pieces?|ea|each|items?|boxes|box|cases?|x|×)"
_STOP = {"unit", "units", "pcs", "piece", "pieces", "ea", "each", "item", "items", "box", "boxes",
         "case", "cases", "of", "on", "for", "from", "at", "to", "and", "the", "line", "po", "sku"}


def _dec(raw: str, what: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as e:
        raise InstructionError(f"Could not read {what} {raw!r} as a number") from e


def _take(pattern: str, text: str) -> tuple[re.Match[str] | None, str]:
    """Find the first match, and return it together with the text with it removed."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None, text
    return m, f"{text[:m.start()]} {text[m.end():]}"


def _take_first(patterns: list[str], text: str) -> tuple[str | None, str]:
    for p in patterns:
        m, rest = _take(p, text)
        if m:
            return next(g for g in m.groups() if g), rest
    return None, text


def _take_date(text: str) -> tuple[str | None, str]:
    return _take_first([r"\b(?:on|dated|date[d:]?|for)?\s*(\d{4}-\d{2}-\d{2})\b"], text)


def _take_po_ref(text: str, *, bare_uuid: bool) -> tuple[str | None, str]:
    patterns = [
        rf"\b(?:purchase\s+order|po)\s*(?:number|no\.?|#)?\s*:?\s*({_UUID})\b",
        r"\b(PO-[\w-]*\d[\w-]*)",
        rf"\b(?:purchase\s+order|po)\s*(?:number|no\.?|#)?\s*:?\s*({_DOC})",
    ]
    if bare_uuid:
        patterns.append(rf"\b({_UUID})\b")
    return _take_first(patterns, text)


def _take_invoice_ref(text: str, *, bare_uuid: bool) -> tuple[str | None, str]:
    patterns = [
        rf"\binvoice\s*(?:number|no\.?|#)?\s*:?\s*#?({_UUID})\b",
        rf"\binvoice\s*(?:number|no\.?|#)?\s*:?\s*#?({_DOC})",
        rf"\b(?:number|no\.?|#)\s*:?\s*({_DOC})",
    ]
    if bare_uuid:
        patterns.append(rf"\b({_UUID})\b")
    return _take_first(patterns, text)


def _take_vendor(text: str) -> tuple[str | None, str]:
    return _take_first([
        rf"\b(?:from|for|by|with|to)?\s*(?:vendor|supplier)\s*:?\s*({_UUID}|{_CODE})",
        rf"\bfrom\s+({_UUID}|{_CODE})",
    ], text)


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------


def _detect(t: str) -> Intent | None:
    if re.search(r"\bmatch", t):
        return Intent.MATCH_INVOICE
    if re.search(r"\binvoice\b", t) and (
        re.search(r"\b(record|create|register|add|enter|log|submit|new|file|book|raise)\b", t)
        or t.startswith("invoice")
    ):
        return Intent.CREATE_INVOICE
    if re.search(r"\breceiv|\breceipt\b", t):
        return Intent.RECEIVE_GOODS
    # Checked before submit, so "create … and submit" is a create. "order" only
    # counts as a verb at the start ("Order 50 units …"), not in "Submit order PO-1".
    if (re.search(r"\b(create|new|raise|draft|make|place)\b", t) or t.startswith("order ")) and re.search(
        r"\b(po|purchase\s+order|order)\b", t
    ):
        return Intent.CREATE_PO
    if re.search(r"\bsubmit", t):
        return Intent.SUBMIT_PO
    if re.search(r"\b(show|get|view|status|details?|what|check|look|display|describe|find)\b", t):
        return Intent.GET_PO
    return None


# ---------------------------------------------------------------------------
# Per-intent parameter extraction
# ---------------------------------------------------------------------------

_ITEM = re.compile(
    rf"(?P<qty>{_NUM})\s*{_UNITS}?\s*(?:of\s+)?(?:sku\s*[:#]?\s*(?=[A-Za-z0-9]))?(?P<sku>[A-Za-z0-9][\w-]*)"
    rf"\s*(?:at|@|for)\s*\$?\s*(?P<price>{_NUM})"
    r"\s*(?:(?:/|per)\s*(?:unit|each|ea|pc|piece|item)|each|ea|apiece)?",
    re.I,
)
_RECEIPT_SKU = re.compile(
    rf"(?P<qty>{_NUM})\s*{_UNITS}?\s*(?:of\s+)?(?:sku\s*[:#]?\s*(?=[A-Za-z0-9]))?(?P<sku>[A-Za-z0-9][\w-]*)", re.I
)
_RECEIPT_LINE = [
    re.compile(rf"(?P<qty>{_NUM})\s*{_UNITS}?\s*(?:of|on|for)\s+line\s*#?\s*(?P<line>\d+)", re.I),
    re.compile(rf"\bline\s*#?\s*(?P<line>\d+)\s*(?:qty|quantity|x|×|:)?\s*(?P<qty>{_NUM})", re.I),
]
_RECEIVE_ALL = r"\b(all|everything|in full|the rest|remaining|fully|complete(?:ly)?)\b"


def _create_po(text: str) -> dict[str, Any]:
    po_number, text = _take_first(
        [rf"\b(?:numbered|number|called|named|no\.?|#)\s*:?\s*({_DOC})", r"\b(PO-[\w-]*\d[\w-]*)"], text)
    order_date, text = _take_date(text)
    vendor, text = _take_vendor(text)
    if not vendor:
        raise InstructionError("Which vendor is the PO for?", "Add 'from vendor ACME'.")
    items = []
    for m in _ITEM.finditer(text):
        sku = m["sku"]
        if sku.lower() in _STOP:
            raise InstructionError(f"Missing SKU before '{m.group(0).strip()}'",
                                   "Write it as '50 units of SKU-9234 at $12.50/unit'.")
        items.append(LineItem(_dec(m["qty"], "quantity"), sku, _dec(m["price"], "price")))
    if not items:
        raise InstructionError("Could not find any line items (quantity, SKU and unit price)",
                               "Write each item as '50 units of SKU-9234 at $12.50/unit'.")
    return {"vendor": vendor, "items": items, "po_number": po_number, "order_date": order_date}


def _receive(text: str) -> dict[str, Any]:
    po_ref, text = _take_po_ref(text, bare_uuid=True)
    if not po_ref:
        raise InstructionError("Which PO are the goods for?", "Add 'on PO-2026-0001'.")
    received_date, text = _take_date(text)
    received_by, text = _take_first([r"\breceived\s+by\s+([\w.@-]+)", r"\bby\s+([\w.@-]+)"], text)
    text = re.sub(r"^\s*(?:please\s+)?(?:record\s+(?:a\s+)?(?:goods\s+)?receipt\s+(?:of|for)?|receive[ds]?)\b",
                  " ", text, flags=re.I)

    items: list[ReceiptItem] = []
    for pattern in _RECEIPT_LINE:
        for m in pattern.finditer(text):
            items.append(ReceiptItem(_dec(m["qty"], "quantity"), line_number=int(m["line"])))
        text = pattern.sub(" ", text)
    for m in _RECEIPT_SKU.finditer(text):
        sku = m["sku"]
        if sku.lower() in _STOP or not re.search(r"[A-Za-z]", sku):
            continue
        items.append(ReceiptItem(_dec(m["qty"], "quantity"), sku=sku))

    receive_all = not items and re.search(_RECEIVE_ALL, text, re.I) is not None
    if not items and not receive_all:
        raise InstructionError("Could not tell what was received",
                               "Say '4 units of BOLT-M8', '4 of line 1', or 'everything'.")
    return {"po": po_ref, "items": items or None, "receive_all": receive_all or None,
            "received_by": received_by, "received_date": received_date}


def _create_invoice(text: str) -> dict[str, Any]:
    po_ref, text = _take_po_ref(text, bare_uuid=False)
    invoice_date, text = _take_date(text)
    invoice_number, text = _take_invoice_ref(text, bare_uuid=False)
    vendor, text = _take_vendor(text)
    amount_raw, text = _take_first([
        rf"\$\s*({_NUM})",
        rf"\b({_NUM})\s*(?:usd|dollars?|eur|euros?)\b",
        rf"\b(?:amount|total)(?:\s+of)?\s*:?\s*\$?\s*({_NUM})",
        rf"\b(?:for|of|totall?ing|worth)\s+\$?\s*({_NUM})\b",
    ], text)
    missing = [name for name, v in [("invoice number", invoice_number), ("vendor", vendor),
                                    ("amount", amount_raw), ("PO", po_ref)] if not v]
    if missing:
        raise InstructionError(f"Missing {', '.join(missing)} for the invoice",
                               "Write it as 'Record invoice INV-77 from ACME for $100 on PO-2026-0001'.")
    return {"invoice_number": invoice_number, "vendor": vendor, "po": po_ref,
            "amount": _dec(amount_raw, "amount"), "invoice_date": invoice_date}


def _match(text: str) -> dict[str, Any]:
    invoice_ref, text = _take_invoice_ref(text, bare_uuid=True)
    if not invoice_ref:
        raise InstructionError("Which invoice should be matched?", "Write 'Match invoice INV-77 from ACME'.")
    vendor, _ = _take_vendor(text)
    return {"invoice": invoice_ref, "vendor": vendor}


def _po_only(text: str) -> dict[str, Any]:
    po_ref, _ = _take_po_ref(text, bare_uuid=True)
    if not po_ref:
        raise InstructionError("Which PO?", "Refer to it by number, e.g. 'PO-2026-0001'.")
    return {"po": po_ref}


_EXTRACT = {
    Intent.CREATE_PO: _create_po,
    Intent.SUBMIT_PO: _po_only,
    Intent.RECEIVE_GOODS: _receive,
    Intent.GET_PO: _po_only,
    Intent.CREATE_INVOICE: _create_invoice,
    Intent.MATCH_INVOICE: _match,
}


def parse(instruction: str) -> Instruction:
    text = " ".join(instruction.split())
    if not text:
        raise InstructionError("The instruction is empty")
    intent = _detect(text.lower())
    if intent is None:
        # A bare PO reference ("PO-2026-0001") means "show it".
        po_ref, _ = _take_po_ref(text, bare_uuid=True)
        if po_ref:
            return Instruction(Intent.GET_PO, {"po": po_ref})
        raise InstructionError("Could not tell what you want to do",
                               "Start with create, submit, receive, show, record invoice, or match.")
    return Instruction(intent, _EXTRACT[intent](text))
