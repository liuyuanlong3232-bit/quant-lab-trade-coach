"""Manual execution workflow. Confirmation records intent; it never contacts a venue."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from uuid import uuid4

from .audit import AuditLog


def create_manual_order(*, symbol: str, side: str, quantity: float, limit_price: float | None = None, audit: AuditLog | None = None) -> dict:
    if (
        not symbol.strip()
        or side not in ("buy", "sell")
        or not isfinite(quantity)
        or quantity <= 0
        or (limit_price is not None and (not isfinite(limit_price) or limit_price <= 0))
    ):
        raise ValueError("invalid manual order")
    order = {"order_id": str(uuid4()), "symbol": symbol.strip(), "side": side, "quantity": quantity, "limit_price": limit_price, "status": "PENDING_MANUAL_CONFIRMATION", "created_at": datetime.now(timezone.utc).isoformat()}
    if audit:
        audit.append("manual_order_created", **order)
    return order


def confirm_manual_order(order: dict, *, audit: AuditLog | None = None) -> dict:
    if order.get("status") != "PENDING_MANUAL_CONFIRMATION":
        raise ValueError("order is not pending manual confirmation")
    confirmed = {**order, "status": "MANUALLY_CONFIRMED_FOR_EXTERNAL_EXECUTION", "confirmed_at": datetime.now(timezone.utc).isoformat()}
    if audit:
        audit.append("manual_order_confirmed", **confirmed, execution="NOT_PERFORMED_BY_QUANT_LAB")
    return confirmed
