"""Расчёт остатков. Итоги не хранятся, а считаются из записей — нечего «перезаписывать»."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Adjustment, Order, Payment, PaymentAllocation


def _sum(s: Session, stmt) -> int:
    return s.execute(stmt).scalar() or 0


def allocated_to_order(s: Session, order_id: int) -> int:
    return _sum(s, select(func.sum(PaymentAllocation.amount_minor)).where(
        PaymentAllocation.order_id == order_id, PaymentAllocation.released_at.is_(None)))


def adjusted_for_order(s: Session, order_id: int) -> int:
    return _sum(s, select(func.sum(Adjustment.amount_minor)).where(Adjustment.order_id == order_id))


def order_debt(s: Session, order: Order) -> int:
    if order.status != "active":
        return 0
    return order.total_minor - allocated_to_order(s, order.id) - adjusted_for_order(s, order.id)


def open_orders(s: Session, client_id: int) -> list[tuple[Order, int]]:
    orders = s.scalars(select(Order).where(
        Order.client_id == client_id, Order.status == "active"
    ).order_by(Order.created_at, Order.id))
    result = []
    for o in orders:
        d = order_debt(s, o)
        if d > 0:
            result.append((o, d))
    return result


def client_debt(s: Session, client_id: int) -> int:
    return sum(d for _, d in open_orders(s, client_id))


def client_credit(s: Session, client_id: int) -> int:
    """Переплата: получено − возвращено − распределено по активным заказам."""
    paid = _sum(s, select(func.sum(Payment.amount_minor)).where(
        Payment.client_id == client_id, Payment.kind == "payment"))
    refunded = _sum(s, select(func.sum(Payment.amount_minor)).where(
        Payment.client_id == client_id, Payment.kind == "refund"))
    alloc = _sum(s, select(func.sum(PaymentAllocation.amount_minor)).join(
        Payment, Payment.id == PaymentAllocation.payment_id).where(
        Payment.client_id == client_id, PaymentAllocation.released_at.is_(None)))
    return paid - refunded - alloc


def payment_remaining(s: Session, payment: Payment) -> int:
    used = _sum(s, select(func.sum(PaymentAllocation.amount_minor)).where(
        PaymentAllocation.payment_id == payment.id, PaymentAllocation.released_at.is_(None)))
    return payment.amount_minor - used
