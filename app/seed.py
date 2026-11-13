"""Seed the dev database with 3 vendors across 2 categories and 5 SKUs.

The spec has no SKU entity: a SKU is a code on a PO line (§2.4). So this script
defines the 5 SKUs in ``SKUS`` and uses them on one sample PO per vendor.

Usage:
    python -m app.seed           # idempotent: rows that already exist are skipped
    python -m app.seed --reset   # drop every table, recreate it, then seed
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import init_db, session_scope
from app.models import (
    EntityType,
    PaymentTerms,
    POLineItem,
    POStatus,
    PurchaseOrder,
    StatusHistory,
    ZERO_QTY,
    Vendor,
    VendorCategory,
)

ACTOR = "seed"


@dataclass(frozen=True)
class Sku:
    code: str
    description: str
    unit_price: Decimal
    category: VendorCategory


SKUS: dict[str, Sku] = {
    s.code: s
    for s in [
        Sku("BOLT-M8", "M8 hex bolt, box of 100", Decimal("25.00"), VendorCategory.GOODS),
        Sku("PANEL-A", "Steel panel 1x2 m", Decimal("100.00"), VendorCategory.GOODS),
        Sku("GLOVE-L", "Work gloves, size L", Decimal("8.50"), VendorCategory.GOODS),
        Sku("SVC-INSTALL", "On-site installation, per hour", Decimal("85.00"), VendorCategory.SERVICES),
        Sku("SVC-MAINT", "Preventive maintenance visit", Decimal("240.00"), VendorCategory.SERVICES),
    ]
}

VENDORS = [
    dict(code="ACME", name="Acme Industrial Supplies",
         payment_terms=PaymentTerms.NET30, category=VendorCategory.GOODS),
    dict(code="BOLTCO", name="BoltCo Fasteners",
         payment_terms=PaymentTerms.NET60, category=VendorCategory.GOODS),
    dict(code="FIXIT", name="FixIt Field Services",
         payment_terms=PaymentTerms.NET30, category=VendorCategory.SERVICES),
]

# (po_number, vendor_code, order_date, target status, [(sku, qty)])
PURCHASE_ORDERS = [
    # Matches the shared setup of the acceptance scenarios (§8): 750.00 total.
    ("PO-2026-0001", "ACME", date(2026, 11, 1), POStatus.SUBMITTED,
     [("BOLT-M8", "10"), ("PANEL-A", "5")]),
    ("PO-2026-0002", "BOLTCO", date(2026, 11, 3), POStatus.DRAFT,
     [("BOLT-M8", "20"), ("GLOVE-L", "50")]),
    ("PO-2026-0003", "FIXIT", date(2026, 11, 5), POStatus.SUBMITTED,
     [("SVC-INSTALL", "8"), ("SVC-MAINT", "2")]),
]


def _history(db: Session, po: PurchaseOrder, from_status: str | None, to_status: str) -> None:
    db.add(StatusHistory(entity_type=EntityType.PURCHASE_ORDER, entity_id=po.id,
                         from_status=from_status, to_status=to_status, actor=ACTOR))


def seed_vendors(db: Session) -> dict[str, Vendor]:
    vendors: dict[str, Vendor] = {}
    for data in VENDORS:
        vendor = db.scalar(select(Vendor).where(Vendor.code == data["code"]))
        if vendor is None:
            vendor = Vendor(**data)
            db.add(vendor)
            print(f"  + vendor {data['code']} ({data['category']}, {data['payment_terms']})")
        else:
            print(f"  = vendor {data['code']} already exists")
        vendors[data["code"]] = vendor
    db.flush()
    return vendors


def seed_purchase_orders(db: Session, vendors: dict[str, Vendor]) -> None:
    for po_number, vendor_code, order_date, status, items in PURCHASE_ORDERS:
        if db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == po_number)):
            print(f"  = PO {po_number} already exists")
            continue

        vendor = vendors[vendor_code]
        po = PurchaseOrder(po_number=po_number, vendor=vendor, order_date=order_date)
        for n, (sku_code, qty) in enumerate(items, start=1):
            sku = SKUS[sku_code]
            assert sku.category == vendor.category, f"{sku_code} does not fit {vendor_code}"
            po.lines.append(POLineItem(line_number=n, sku=sku.code, description=sku.description,
                                       quantity_ordered=Decimal(qty), unit_price=sku.unit_price,
                                       quantity_received=ZERO_QTY))
        db.add(po)
        db.flush()  # assigns po.id, used by the history rows
        _history(db, po, None, POStatus.DRAFT)

        if status is POStatus.SUBMITTED:
            po.status = POStatus.SUBMITTED
            _history(db, po, POStatus.DRAFT, POStatus.SUBMITTED)

        total = sum((pl.line_total for pl in po.lines), Decimal("0"))
        print(f"  + PO {po_number} for {vendor_code}: {len(po.lines)} lines, {total}, {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    args = parser.parse_args()

    init_db(drop=args.reset)
    with session_scope() as db:
        print("Seeding vendors…")
        vendors = seed_vendors(db)
        print("Seeding purchase orders…")
        seed_purchase_orders(db, vendors)
    print(f"Done. SKUs available: {', '.join(SKUS)}")


if __name__ == "__main__":
    main()
