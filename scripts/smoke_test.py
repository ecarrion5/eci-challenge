"""Smoke test against a running server. Safe to rerun: each run uses fresh document numbers.

    python -m app.seed                  # once (or --reset for a clean database)
    uvicorn app.main:app                # in another terminal
    python scripts/smoke_test.py [BASE_URL]   # default http://127.0.0.1:8000/api/v1

Vendor and seeded-PO ids are read from the database (there is no vendor API yet), so
run this with the same DATABASE_URL as the server. Exits with status 1 if a check fails.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models import PaymentTerms, PurchaseOrder, Vendor, VendorCategory  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1").rstrip("/")
RUN = time.strftime("%H%M%S")
failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-User": "smoke-test"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name: str, res: tuple[int, dict], http: int, /, **expect) -> dict:
    """Assert the HTTP status and dotted-path fields, e.g. error__code="DUPLICATE"."""
    code, body = res
    problems = [] if code == http else [f"HTTP {code} != {http}"]
    for key, want in expect.items():
        got = body
        for part in key.split("__"):
            got = got[int(part)] if isinstance(got, list) else (got or {}).get(part)
        if got != want:
            problems.append(f"{key}={got!r} != {want!r}")
    mark = "PASS" if not problems else "FAIL"
    print(f"  {mark}  {name}" + ("" if not problems else f"  ({'; '.join(problems)})"))
    if problems:
        failures.append(name)
        if "error" in body:
            print("        ", json.dumps(body["error"])[:400])
    return body


def lookup_ids() -> tuple[dict[str, str], str | None]:
    with session_scope() as db:
        vendors = {v.code: v.id for v in db.scalars(select(Vendor))}
        if "SMOKE_INACTIVE" not in vendors:  # fixture for the inactive-vendor rule
            v = Vendor(code="SMOKE_INACTIVE", name="Smoke-test inactive vendor", active=False,
                       payment_terms=PaymentTerms.NET30, category=VendorCategory.GOODS)
            db.add(v)
            db.flush()
            vendors[v.code] = v.id
        seeded = db.scalar(select(PurchaseOrder.id).where(PurchaseOrder.po_number == "PO-2026-0001"))
    missing = {"ACME", "BOLTCO"} - vendors.keys()
    if missing:
        sys.exit(f"Seed vendors {missing} not found. Run: python -m app.seed")
    return vendors, seeded


def main() -> None:
    vendors, seeded_po = lookup_ids()
    acme, boltco, inactive = vendors["ACME"], vendors["BOLTCO"], vendors["SMOKE_INACTIVE"]
    print(f"Smoke test against {BASE} (run {RUN})")

    print("health & seed")
    check("GET /health", call("GET", "/health"), 200, status="ok")
    if seeded_po:
        check("GET seeded PO-2026-0001", call("GET", f"/purchase-orders/{seeded_po}"), 200,
              po_number="PO-2026-0001", status="SUBMITTED", vendor__code="ACME")

    print("purchase orders")
    lines = [{"line_number": 1, "sku": "BOLT-M8", "quantity_ordered": "10", "unit_price": "25.00"},
             {"line_number": 2, "sku": "PANEL-A", "quantity_ordered": "5", "unit_price": "100.00"}]
    po_body = {"po_number": f"SMOKE-{RUN}", "vendor_id": acme, "order_date": "2026-11-01", "lines": lines}
    check("create PO for inactive vendor → 422",
          call("POST", "/purchase-orders", {**po_body, "vendor_id": inactive}), 422,
          error__code="BUSINESS_RULE_VIOLATION", error__details__0__rule="R3")
    po = check("create draft PO", call("POST", "/purchase-orders", po_body), 201,
               status="DRAFT", receipt_status="NOT_RECEIVED", lines__0__line_total="250.00")
    check("duplicate po_number → 409", call("POST", "/purchase-orders", po_body), 409,
          error__code="DUPLICATE")
    pid, l1, l2 = po["id"], po["lines"][0]["id"], po["lines"][1]["id"]

    def receive(*items: tuple[str, str]) -> tuple[int, dict]:
        return call("POST", f"/purchase-orders/{pid}/receive", {
            "received_by": "smoke-test", "received_date": "2026-11-09",
            "lines": [{"po_line_id": i, "quantity_received": q} for i, q in items]})

    check("receive on DRAFT → 409", receive((l1, "1")), 409, error__code="INVALID_TRANSITION")
    check("submit", call("POST", f"/purchase-orders/{pid}/submit"), 200, status="SUBMITTED")
    check("submit again → 409", call("POST", f"/purchase-orders/{pid}/submit"), 409,
          error__code="INVALID_TRANSITION")

    print("partial receipt")
    check("receive 4 of 10 on line 1", receive((l1, "4")), 201,
          purchase_order__status="SUBMITTED", purchase_order__receipt_status="PARTIALLY_RECEIVED",
          purchase_order__lines__0__quantity_received="4.0000",
          purchase_order__lines__0__quantity_remaining="6.0000",
          purchase_order__lines__0__receipt_status="PARTIALLY_RECEIVED",
          purchase_order__lines__1__receipt_status="NOT_RECEIVED")
    check("receive 7 more on line 1 (only 6 left) → 422", receive((l1, "7")), 422,
          error__code="BUSINESS_RULE_VIOLATION", error__details__0__rule="R6")
    check("GET detail: rejected receipt changed nothing", call("GET", f"/purchase-orders/{pid}"), 200,
          lines__0__quantity_received="4.0000", goods_receipts__0__lines__0__quantity_received="4.0000")

    print("invoices & match (received value 100.00)")
    inv = lambda num, amount, vendor=acme: {"vendor_id": vendor, "po_id": pid,
                                            "invoice_number": f"{num}-{RUN}", "amount": amount}
    check("invoice: PO belongs to another vendor → 400",
          call("POST", "/invoices", inv("SMK-X", "1.00", boltco)), 400, error__code="PO_VENDOR_MISMATCH")
    no_vendor = inv("SMK-X", "1.00")
    del no_vendor["vendor_id"]
    check("invoice without vendor_id → 422", call("POST", "/invoices", no_vendor), 422,
          error__code="VALIDATION_ERROR", error__details__0__field="vendor_id")
    small = check("create invoice 100.00", call("POST", "/invoices", inv("SMK-A", "100")), 201,
                  status="PENDING", amount="100.00")
    check("match 100.00 ≤ 100.00 → MATCHED, partial pending",
          call("POST", f"/invoices/{small['id']}/match"), 200, invoice_status="MATCHED",
          received_value="100.00", partial_receipt_pending=True)
    big = check("create invoice 750.00", call("POST", "/invoices", inv("SMK-B", "750.00")), 201)
    check("match 750.00 > 100.00 → 422, stays PENDING",
          call("POST", f"/invoices/{big['id']}/match"), 422, error__code="AMOUNT_EXCEEDS_RECEIVED",
          error__data__invoice_status="PENDING", error__data__received_value="100.00",
          error__data__excess="650.00", error__data__partial_receipt_pending=True)
    check("duplicate invoice number (other case) → 409",
          call("POST", "/invoices", {**inv("smk-a", "1.00")}), 409, error__code="DUPLICATE")

    print("full receipt")
    check("receive the rest → PO RECEIVED", receive((l1, "6"), (l2, "5")), 201,
          purchase_order__status="RECEIVED", purchase_order__receipt_status="FULLY_RECEIVED")
    check("receive on RECEIVED PO → 409", receive((l1, "1")), 409, error__code="INVALID_TRANSITION")
    check("re-match 750.00 → MATCHED, nothing pending",
          call("POST", f"/invoices/{big['id']}/match"), 200, invoice_status="MATCHED",
          received_value="750.00", partial_receipt_pending=False)

    agent_checks()

    print(f"\n{'FAILED' if failures else 'OK'}: {len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    sys.exit(1 if failures else 0)


def agent_checks() -> None:
    """The same lifecycle, driven through POST /agent/instruct in plain English."""

    def say(text: str, http: int = 200, **expect) -> dict:
        body = check(f"agent: {text}", call("POST", "/agent/instruct", {"instruction": text}), http, **expect)
        print(f"        → {body.get('summary') or body.get('error', {}).get('message')}")
        return body

    print("agent (POST /agent/instruct)")
    created = say("Create a PO for 50 units of SKU-9234 from vendor ACME at $12.50/unit",
                  intent="create_po", api_response__status_code=201,
                  api_response__body__lines__0__line_total="625.00")
    po = created.get("api_response", {}).get("body", {}).get("po_number", "PO-MISSING")
    say(f"Submit {po}", intent="submit_po", api_response__body__status="SUBMITTED")
    say(f"Receive 20 units of SKU-9234 on {po}", intent="receive_goods", api_response__status_code=201,
        api_response__body__purchase_order__receipt_status="PARTIALLY_RECEIVED")
    say(f"Receive 40 units of SKU-9234 on {po}", api_response__status_code=422,
        api_response__body__error__details__0__rule="R6")
    say(f"Record invoice AG-A-{RUN} from ACME for $250 on {po}", intent="create_invoice",
        api_response__status_code=201, api_response__body__status="PENDING")
    say(f"Match invoice AG-A-{RUN} from ACME", intent="match_invoice", api_response__status_code=200,
        api_response__body__invoice_status="MATCHED", api_response__body__partial_receipt_pending=True)
    say(f"Record invoice AG-B-{RUN} from ACME for $625 on {po}", api_response__status_code=201)
    say(f"Match invoice AG-B-{RUN}", api_response__status_code=422,
        api_response__body__error__code="AMOUNT_EXCEEDS_RECEIVED")
    say(f"Record invoice AG-C-{RUN} from BOLTCO for $1 on {po}", api_response__status_code=400,
        api_response__body__error__code="PO_VENDOR_MISMATCH")
    say(f"Receive everything on {po}", api_response__status_code=201,
        api_response__body__purchase_order__status="RECEIVED")
    say(f"Receive everything on {po}", 409, error__code="INVALID_TRANSITION")
    say(f"Show {po}", intent="get_po", api_response__body__receipt_status="FULLY_RECEIVED")
    say("Create a PO for 5 units of X-1 at $1 from vendor SMOKE_INACTIVE", api_response__status_code=422,
        api_response__body__error__details__0__rule="R3")
    say("Create a PO for 5 units of X-1 at $1 from vendor NOPE", 404, error__code="NOT_FOUND")
    say("do a backflip", 422, error__code="INSTRUCTION_NOT_UNDERSTOOD")


if __name__ == "__main__":
    main()
