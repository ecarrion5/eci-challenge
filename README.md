# PO / Invoice 3-Way Match Agent

A small FastAPI service for purchase-to-pay. It manages purchase orders (POs) from draft to receipt of goods. It registers vendor invoices against those POs and checks them with a **3-way match**: an invoice passes only if its amount is covered by the value of the goods actually received. A plain-English **agent endpoint** lets you drive the whole flow with sentences like *"Create a PO for 50 units of SKU-9234 from vendor ACME at $12.50/unit"*.

The full specification (entities, state machines, endpoints, validation rules, open questions) is in [SPEC.md](SPEC.md).

## What it does

**Purchase orders**
- Create a draft PO for a vendor, with line items (SKU, quantity, unit price). Inactive vendors can't get new POs.
- Submit a draft PO for fulfillment.
- Record goods receipts, including partial receipts. A line can never be received beyond its ordered quantity. The PO becomes `RECEIVED` when every line is complete.
- View a PO with the receipt status of every line: received, remaining, and `NOT_RECEIVED`, `PARTIALLY_RECEIVED` or `FULLY_RECEIVED`.

**Invoices and the 3-way match**
- Register an invoice (`vendor_id`, `po_id`, `invoice_number`, `amount`). It is rejected with **400** if the PO belongs to a different vendor.
- Match it. The service computes `received_value = Σ quantity_received × unit_price` over the PO's lines.
  - `amount > received_value` → **422** with a structured error body, and the invoice stays `PENDING`.
  - Otherwise the invoice becomes `MATCHED`.
  - `partial_receipt_pending` reports whether goods are still outstanding. It is informational only and doesn't affect pass or fail.

**Agent**
- `POST /api/v1/agent/instruct` parses an instruction with a deterministic, rule-based parser (no LLM or API key needed). It then calls the matching endpoint of this same API in-process and returns the API's response plus a one-line summary.

**Throughout**
- Each request is one database transaction, committed before the response is sent. Errors roll back everything.
- Receipts and matches lock the PO, so concurrent requests can't over-receive.
- Every status change is recorded in an audit history, with the actor taken from the `X-User` header.
- Money and quantities are exact decimals, sent in JSON as strings (`"12.50"`).

## Quick start

Requires Python 3.11+ (developed on 3.12).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m app.seed --reset         # create ./eci.db with sample data
uvicorn app.main:app --reload      # http://127.0.0.1:8000
```

Open **http://127.0.0.1:8000/docs** for interactive API docs, where you can try every endpoint.

The seed creates:

| Vendors | SKUs | POs |
|---|---|---|
| `ACME` (goods, NET30)<br>`BOLTCO` (goods, NET60)<br>`FIXIT` (services, NET30) | `BOLT-M8`, `PANEL-A`, `GLOVE-L`, `SVC-INSTALL`, `SVC-MAINT` | `PO-2026-0001` (ACME, submitted, 750.00)<br>`PO-2026-0002` (BOLTCO, draft)<br>`PO-2026-0003` (FIXIT, submitted) |

Running `python -m app.seed` without `--reset` is safe to repeat; it skips rows that already exist. Use `DATABASE_URL` to point at a different database (default `sqlite:///./eci.db`).

## Endpoints

All paths are under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and database check |
| POST | `/purchase-orders` | Create a draft PO with line items |
| POST | `/purchase-orders/{id}/submit` | Submit for fulfillment |
| POST | `/purchase-orders/{id}/receive` | Record a (partial) goods receipt |
| GET | `/purchase-orders/{id}` | PO detail with receipt status per line |
| POST | `/invoices` | Register an invoice (`PENDING`) |
| POST | `/invoices/{id}/match` | Run the 3-way match |
| POST | `/agent/instruct` | Run a plain-English instruction |

Errors use one envelope: `{"error": {"code", "message", "details": [{"field", "rule", "message"}], "data"?}}`. The codes and rules are listed in [SPEC.md §4.3 and §5](SPEC.md).

## How to test it

### 1. Smoke test (recommended)

With the server running, in a second terminal:

```bash
python scripts/smoke_test.py                      # or: python scripts/smoke_test.py http://host:port/api/v1
```

It runs 36 checks against the live server and exits with status 1 if any fail:
- **21 direct API checks:** the full PO lifecycle, partial and over-receipt, invoice/vendor mismatch, a failing then passing match, duplicates, and status errors.
- **15 agent checks:** the same lifecycle in plain English, plus unknown-vendor and unparseable instructions.

Each run uses fresh document numbers, so it can be rerun at any time.

Expected output (abridged):
```
  PASS  receive 7 more on line 1 (only 6 left) → 422
  PASS  match 750.00 > 100.00 → 422, stays PENDING
  ...
  PASS  agent: Create a PO for 50 units of SKU-9234 from vendor ACME at $12.50/unit
        → Created draft PO PO-AG-…-EF24 for ACME: 50 × SKU-9234 at 12.50 (total 625.00); submit it to send it for fulfillment.

OK: 0 failure(s)
```

Notes:
- The script reads vendor ids from the database (there is no vendor API yet), so run it with the same `DATABASE_URL` as the server.
- On its first run it adds an inactive vendor, `SMOKE_INACTIVE`, to test the inactive-vendor rule.
- Test data accumulates; run `python -m app.seed --reset` for a clean database.

### 2. Try the agent

```bash
say() { curl -s localhost:8000/api/v1/agent/instruct -H 'Content-Type: application/json' \
          -d "{\"instruction\": \"$1\"}" | python -m json.tool; }

say "Create a PO for 50 units of SKU-9234 from vendor ACME at \$12.50/unit number PO-DEMO-1"
say "Submit PO-DEMO-1"
say "Receive 20 units of SKU-9234 on PO-DEMO-1"
say "Show PO-DEMO-1"
say "Record invoice INV-DEMO-1 from ACME for \$250 on PO-DEMO-1"
say "Match invoice INV-DEMO-1 from ACME"          # passes: 250.00 received
say "Record invoice INV-DEMO-2 from ACME for \$625 on PO-DEMO-1"
say "Match invoice INV-DEMO-2 from ACME"          # fails: only 250.00 received
say "Receive everything on PO-DEMO-1"
say "Match invoice INV-DEMO-2 from ACME"          # now passes
```

Each response contains:
- `intent` and the extracted `parameters`;
- the exact `api_call` made;
- the untouched `api_response`, with its status code;
- a one-line `summary`.

The agent returns 200 whenever it called the API, even if the API rejected the request; `api_response.status_code` shows the API's own answer. It fails on its own (422 `INSTRUCTION_NOT_UNDERSTOOD`, 404 or 409) only when it can't build a call. All supported phrasings are listed in [SPEC.md §10](SPEC.md).

### 3. Call the API directly

```bash
curl -s localhost:8000/api/v1/purchase-orders -H 'Content-Type: application/json' -H 'X-User: me' -d '{
  "po_number": "PO-CURL-1", "vendor_id": "<ACME id>", "order_date": "2026-11-01",
  "lines": [{"line_number": 1, "sku": "BOLT-M8", "quantity_ordered": "10", "unit_price": "25.00"}]
}'
```

Vendor ids are UUIDs created by the seed, and there is no vendor API yet. The quickest way to get ACME's id is through the agent:
`say "Show PO-2026-0001"`, then read `api_response.body.vendor_id`. The `id`s of the PO and its lines are in the same response.

### 4. Clean up when you're done

1. **Stop the server.** Press `Ctrl+C` in the terminal running uvicorn. If it runs in the background, use:
   ```bash
   kill $(pgrep -f "uvicorn app.main:app")
   ```
   Stop it before step 2, so nothing is writing to the database.
2. **Remove the test data (optional).** Testing leaves POs, invoices and the `SMOKE_INACTIVE` vendor in `eci.db`. Either:
   ```bash
   python -m app.seed --reset          # back to just the seed data
   rm eci.db eci.db-shm eci.db-wal     # or delete the database entirely
   ```
   After a delete, the server recreates empty tables on its next start, and `python -m app.seed` refills them.
3. **Clear the Python caches (optional).** They are harmless and get recreated:
   ```bash
   find app -name __pycache__ -exec rm -rf {} +
   ```

Keep `.venv` if you plan to run the project again.

## Project layout

```text
app/
  main.py             FastAPI app, routers, error handlers, /health
  database.py         engine, sessions (get_db, session_scope), init_db
  deps.py             request-scoped DB session and X-User actor
  models.py           SQLAlchemy models
  schemas.py          Pydantic request/response models and the error envelope
  errors.py           domain errors → error envelope
  state_machine.py    allowed status transitions + audit history
  po_service.py       PO create / submit / receive / detail
  invoice_service.py  invoice create / match
  routers/            purchase_orders.py, invoices.py, agent.py
  agent/              parser.py (rules), executor.py (resolve + summarize), asgi_client.py
  seed.py             sample data
scripts/smoke_test.py end-to-end checks against a running server
SPEC.md               full specification
```

## Known limitations

- **Over-matching across invoices.** Each invoice is compared with the PO's *total* received value, ignoring other invoices already matched on the same PO, so several invoices can together match more than was received. A fix is proposed in [SPEC.md §9](SPEC.md).
- **Not built yet:** approve and mark-paid (with GL posting), PO close, list and lookup endpoints, a vendor API, and idempotency keys. See the "Planned" table in SPEC.md.
- **Schema changes:** tables are created at startup with `create_all`, which doesn't alter existing tables. After a model change, run `python -m app.seed --reset`. There are no migrations yet.
- **Tests:** there is no pytest suite yet. The smoke test is the end-to-end check.
- **Agent phrasing:** the agent understands phrasings close to the documented patterns. It rejects anything else with a hint instead of guessing.
