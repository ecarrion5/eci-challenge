"""SQLAlchemy ORM models for the entities in SPEC.md §2."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.types import TypeDecorator

from app.database import Base

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaymentTerms(StrEnum):
    NET30 = "NET30"
    NET60 = "NET60"

    @property
    def days(self) -> int:
        return {PaymentTerms.NET30: 30, PaymentTerms.NET60: 60}[self]


class VendorCategory(StrEnum):
    GOODS = "GOODS"
    SERVICES = "SERVICES"


class POStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    RECEIVED = "RECEIVED"
    CLOSED = "CLOSED"


class InvoiceStatus(StrEnum):
    PENDING = "PENDING"
    MATCHED = "MATCHED"
    APPROVED = "APPROVED"
    PAID = "PAID"


class ReceiptStatus(StrEnum):
    """Derived receipt progress of a PO line or a whole PO. Not stored."""

    NOT_RECEIVED = "NOT_RECEIVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    FULLY_RECEIVED = "FULLY_RECEIVED"


class EntityType(StrEnum):
    PURCHASE_ORDER = "PURCHASE_ORDER"
    INVOICE = "INVOICE"


# Invoice statuses whose lines count towards a PO line's invoiced quantity.
INVOICED_STATUSES = frozenset({InvoiceStatus.APPROVED, InvoiceStatus.PAID})


def _enum(enum_cls: type[StrEnum]) -> SAEnum:
    # Stored as VARCHAR plus a CHECK constraint: portable, and easy to extend later.
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=32,
        name=enum_cls.__name__.lower(),
    )


# ---------------------------------------------------------------------------
# Column types and helpers
# ---------------------------------------------------------------------------

MONEY_Q = Decimal("0.01")
QTY_Q = Decimal("0.0001")
ZERO_QTY = Decimal("0.0000")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExactDecimal(TypeDecorator):
    """Fixed-scale ``Decimal`` that is exact on every backend.

    SQLite has no real DECIMAL type: NUMERIC columns can come back as binary
    floats. On SQLite the quantized value is stored as TEXT. Other backends use
    ``NUMERIC(precision, scale)``. Note that ORDER BY and SUM on these columns are
    not numeric on SQLite, so do the arithmetic in Python.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__(precision, scale)
        self.precision = precision
        self.scale = scale
        self.quantum = Decimal(1).scaleb(-scale)

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self.precision + 2))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            value = str(value)
        q = Decimal(value).quantize(self.quantum, rounding=ROUND_HALF_UP)
        return str(q) if dialect.name == "sqlite" else q

    def process_result_value(self, value: Any, dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value)).quantize(self.quantum)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes. SQLite drops tzinfo, so it is reattached on load."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


Money = ExactDecimal(14, 2)
Quantity = ExactDecimal(14, 4)


def _uuid_pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=_new_id)


# ---------------------------------------------------------------------------
# Vendor (§2.2)
# ---------------------------------------------------------------------------


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    payment_terms: Mapped[PaymentTerms] = mapped_column(_enum(PaymentTerms), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[VendorCategory] = mapped_column(_enum(VendorCategory), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    purchase_orders: Mapped[list[PurchaseOrder]] = relationship(back_populates="vendor")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="vendor")

    def __repr__(self) -> str:
        return f"<Vendor {self.code}>"


# ---------------------------------------------------------------------------
# PurchaseOrder / POLineItem (§2.3, §2.4)
# ---------------------------------------------------------------------------


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = _uuid_pk()
    po_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True, nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[POStatus] = mapped_column(
        _enum(POStatus), default=POStatus.DRAFT, index=True, nullable=False
    )
    # Optimistic lock: SQLAlchemy increments it on every UPDATE of this row and
    # raises StaleDataError on a mismatch. Changes to lines only don't touch this
    # row, so services must also bump updated_at when lines change.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    vendor: Mapped[Vendor] = relationship(back_populates="purchase_orders")
    lines: Mapped[list[POLineItem]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        order_by="POLineItem.line_number",
    )
    goods_receipts: Mapped[list[GoodsReceipt]] = relationship(
        back_populates="purchase_order", order_by="GoodsReceipt.received_date"
    )
    invoices: Mapped[list[Invoice]] = relationship(back_populates="purchase_order")

    __mapper_args__ = {"version_id_col": version}

    def line(self, po_line_id: str) -> POLineItem | None:
        return next((pl for pl in self.lines if pl.id == po_line_id), None)

    @property
    def fully_received(self) -> bool:
        return bool(self.lines) and all(
            pl.quantity_received >= pl.quantity_ordered for pl in self.lines
        )

    @property
    def receipt_status(self) -> ReceiptStatus:
        if self.fully_received:
            return ReceiptStatus.FULLY_RECEIVED
        if any(pl.quantity_received > 0 for pl in self.lines):
            return ReceiptStatus.PARTIALLY_RECEIVED
        return ReceiptStatus.NOT_RECEIVED

    @property
    def fully_invoiced(self) -> bool:
        return bool(self.lines) and all(pl.invoiced_qty >= pl.quantity_ordered for pl in self.lines)

    def __repr__(self) -> str:
        return f"<PurchaseOrder {self.po_number} {self.status}>"


class POLineItem(Base):
    __tablename__ = "po_line_items"
    __table_args__ = (UniqueConstraint("po_id", "line_number", name="uq_po_line_items_po_line"),)

    id: Mapped[str] = _uuid_pk()
    po_id: Mapped[str] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    quantity_ordered: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    # Cumulative quantity received: the source of truth for receipt status and
    # matching. Every goods receipt increments it while holding the PO lock.
    # GoodsReceiptLine rows are the audit trail behind it; this value is never
    # re-derived from them.
    quantity_received: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=ZERO_QTY
    )

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    gr_lines: Mapped[list[GoodsReceiptLine]] = relationship(back_populates="po_line")
    invoice_lines: Mapped[list[InvoiceLine]] = relationship(back_populates="po_line")

    @validates("quantity_ordered", "unit_price")
    def _recompute_line_total(self, key: str, value: Decimal) -> Decimal:
        qty = value if key == "quantity_ordered" else self.quantity_ordered
        price = value if key == "unit_price" else self.unit_price
        if qty is not None and price is not None:
            self.line_total = round_money(Decimal(qty) * Decimal(price))
        return value

    @property
    def quantity_remaining(self) -> Decimal:
        """How much can still be received. Never negative."""
        return max(self.quantity_ordered - self.quantity_received, Decimal("0"))

    @property
    def receipt_status(self) -> ReceiptStatus:
        received = self.quantity_received
        if received >= self.quantity_ordered:
            return ReceiptStatus.FULLY_RECEIVED
        return ReceiptStatus.PARTIALLY_RECEIVED if received > 0 else ReceiptStatus.NOT_RECEIVED

    @property
    def invoiced_qty(self) -> Decimal:
        """Derived (§2.4): Σ quantities on APPROVED/PAID invoices for this PO line."""
        return sum(
            (il.quantity for il in self.invoice_lines if il.invoice.status in INVOICED_STATUSES),
            Decimal("0"),
        )

    def __repr__(self) -> str:
        return f"<POLineItem {self.line_number} {self.sku}>"


# ---------------------------------------------------------------------------
# GoodsReceipt / GoodsReceiptLine (§2.5)
# ---------------------------------------------------------------------------


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"

    id: Mapped[str] = _uuid_pk()
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True, nullable=False)
    received_by: Mapped[str] = mapped_column(String(64), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="goods_receipts")
    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (UniqueConstraint("gr_id", "po_line_id", name="uq_gr_lines_gr_po_line"),)

    id: Mapped[str] = _uuid_pk()
    gr_id: Mapped[str] = mapped_column(
        ForeignKey("goods_receipts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    po_line_id: Mapped[str] = mapped_column(
        ForeignKey("po_line_items.id"), index=True, nullable=False
    )
    quantity_received: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    po_line: Mapped[POLineItem] = relationship(back_populates="gr_lines")


# ---------------------------------------------------------------------------
# Invoice / InvoiceLine (§2.6)
# ---------------------------------------------------------------------------


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = _uuid_pk()
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True, nullable=False)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True, nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        _enum(InvoiceStatus), default=InvoiceStatus.PENDING, index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    vendor: Mapped[Vendor] = relationship(back_populates="invoices")
    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="invoices")
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    gl_entry: Mapped[GLEntry | None] = relationship(back_populates="invoice", uselist=False)

    @property
    def due_date(self) -> date:
        """Derived (§2.6): invoice_date plus the vendor's payment terms."""
        return self.invoice_date + timedelta(days=PaymentTerms(self.vendor.payment_terms).days)

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} {self.status}>"


# R8: (vendor_id, invoice_number) is unique, ignoring case. Schemas trim whitespace.
Index(
    "uq_invoices_vendor_number_ci",
    Invoice.vendor_id,
    func.lower(Invoice.invoice_number),
    unique=True,
)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "po_line_id", name="uq_invoice_lines_invoice_po_line"),
    )

    id: Mapped[str] = _uuid_pk()
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False
    )
    po_line_id: Mapped[str] = mapped_column(
        ForeignKey("po_line_items.id"), index=True, nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    po_line: Mapped[POLineItem] = relationship(back_populates="invoice_lines")


# ---------------------------------------------------------------------------
# GLEntry (§2.7)
# ---------------------------------------------------------------------------


class GLEntry(Base):
    __tablename__ = "gl_entries"
    __table_args__ = (CheckConstraint("debit = credit", name="balanced"),)

    id: Mapped[str] = _uuid_pk()
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id"), unique=True, nullable=False
    )
    debit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    credit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="gl_entry")


# ---------------------------------------------------------------------------
# StatusHistory (§2.9)
# ---------------------------------------------------------------------------


class StatusHistory(Base):
    __tablename__ = "status_history"
    __table_args__ = (Index("ix_status_history_entity", "entity_type", "entity_id"),)

    id: Mapped[str] = _uuid_pk()
    entity_type: Mapped[EntityType] = mapped_column(_enum(EntityType), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)  # polymorphic: no FK
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
