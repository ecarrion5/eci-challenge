"""Purchase order endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app import po_service
from app.deps import Actor, DbSession
from app.schemas import ErrorResponse, GRCreate, GRRead, POCreate, PODetail, ReceiveResponse

router = APIRouter(prefix="/api/v1/purchase-orders", tags=["purchase-orders"])


def _errors(*codes: int) -> dict:
    return {code: {"model": ErrorResponse} for code in codes}


def _detail(db, po_id: str) -> PODetail:
    return PODetail.model_validate(po_service.get_po_detail(db, po_id))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PODetail,
             responses=_errors(404, 409, 422), summary="Create a DRAFT purchase order")
def create_purchase_order(body: POCreate, db: DbSession, actor: Actor) -> PODetail:
    """Creates a DRAFT PO for a vendor with at least one line item.

    Rejected when the vendor doesn't exist (404) or is inactive (422), when `po_number`
    is already used (409), or when line numbers repeat (422).
    """
    po = po_service.create_po(db, body, actor)
    return PODetail.model_validate(po)


@router.post("/{po_id}/submit", response_model=PODetail, responses=_errors(404, 409, 422),
             summary="Submit a DRAFT purchase order for fulfillment")
def submit_purchase_order(po_id: UUID, db: DbSession, actor: Actor) -> PODetail:
    """DRAFT → SUBMITTED. Refused when the PO isn't DRAFT (409) or its vendor has
    been deactivated since the draft was created (422)."""
    po_service.submit_po(db, str(po_id), actor)
    return _detail(db, str(po_id))


@router.post("/{po_id}/receive", status_code=status.HTTP_201_CREATED,
             response_model=ReceiveResponse, responses=_errors(404, 409, 422),
             summary="Record a goods receipt (partial receipts allowed)")
def receive_purchase_order(po_id: UUID, body: GRCreate, db: DbSession, actor: Actor) -> ReceiveResponse:
    """Records the received quantities for any subset of lines. The PO must be SUBMITTED (409).
    No line can be received beyond its ordered quantity (422). The PO becomes RECEIVED
    once every line has been fully received.
    """
    gr = po_service.receive_po(db, str(po_id), body, actor)
    return ReceiveResponse(goods_receipt=GRRead.model_validate(gr),
                           purchase_order=PODetail.model_validate(gr.purchase_order))


@router.get("/{po_id}", response_model=PODetail, responses=_errors(404),
            summary="Purchase order detail with receipt status per line")
def get_purchase_order(po_id: UUID, db: DbSession) -> PODetail:
    return _detail(db, str(po_id))
