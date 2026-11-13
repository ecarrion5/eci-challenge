"""Status transitions for PurchaseOrder and Invoice (SPEC.md §3), with audit history.

Every status change goes through ``transition``, which refuses any move not
listed below and writes exactly one StatusHistory row in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.errors import InvalidTransition
from app.models import EntityType, Invoice, InvoiceStatus, POStatus, PurchaseOrder, StatusHistory

_PO = POStatus
_INV = InvoiceStatus

# from_status (None = creation) -> allowed to_statuses
TRANSITIONS: dict[EntityType, dict[str | None, frozenset[str]]] = {
    EntityType.PURCHASE_ORDER: {
        None: frozenset({_PO.DRAFT}),
        _PO.DRAFT: frozenset({_PO.SUBMITTED}),
        _PO.SUBMITTED: frozenset({_PO.RECEIVED, _PO.CLOSED}),
        _PO.RECEIVED: frozenset({_PO.CLOSED}),
    },
    EntityType.INVOICE: {
        None: frozenset({_INV.PENDING}),
        _INV.PENDING: frozenset({_INV.MATCHED}),
        _INV.MATCHED: frozenset({_INV.APPROVED}),
        _INV.APPROVED: frozenset({_INV.PAID}),
    },
}

Tracked = PurchaseOrder | Invoice


def _entity_type(entity: Tracked) -> EntityType:
    if isinstance(entity, PurchaseOrder):
        return EntityType.PURCHASE_ORDER
    if isinstance(entity, Invoice):
        return EntityType.INVOICE
    raise TypeError(f"{type(entity).__name__} has no state machine")


def _label(entity: Tracked) -> str:
    if isinstance(entity, PurchaseOrder):
        return f"Purchase order {entity.po_number}"
    return f"Invoice {entity.invoice_number}"


def _write_history(db: Session, entity: Tracked, from_status: str | None, to_status: str,
                   actor: str, reason: str | None) -> None:
    if entity.id is None:
        db.flush()  # the history row needs the entity's id
    db.add(StatusHistory(entity_type=_entity_type(entity), entity_id=entity.id,
                         from_status=from_status, to_status=to_status, actor=actor, reason=reason))


def record_created(db: Session, entity: Tracked, actor: str) -> None:
    """Write the creation row (from_status = null) for a newly added entity."""
    allowed = TRANSITIONS[_entity_type(entity)][None]
    if entity.status not in allowed:
        raise InvalidTransition(f"{_label(entity)} cannot be created in status {entity.status}")
    _write_history(db, entity, None, entity.status, actor, None)


def transition(db: Session, entity: Tracked, to_status: str, actor: str,
               reason: str | None = None) -> None:
    from_status = entity.status
    allowed = TRANSITIONS[_entity_type(entity)].get(from_status, frozenset())
    if to_status not in allowed:
        raise InvalidTransition(f"{_label(entity)} cannot move from {from_status} to {to_status}")
    entity.status = to_status
    _write_history(db, entity, from_status, to_status, actor, reason)


def ensure_status(entity: Tracked, allowed: Iterable[str], action: str) -> None:
    """Guard an action that needs the entity in one of ``allowed`` statuses."""
    allowed = list(allowed)
    if entity.status not in allowed:
        raise InvalidTransition(
            f"Cannot {action}: {_label(entity)} is {entity.status} "
            f"(must be {' or '.join(allowed)})"
        )
