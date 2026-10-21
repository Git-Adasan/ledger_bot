from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Adjustment, Client, Order, Payment, PaymentAllocation
from ..errors import ServiceError
from . import balance


@dataclass
class PaymentResult:
    payment: Payment
    allocations: list  # [(order_id, amount_minor)]
    credit_after: int
    duplicate: bool


def _allocs_of(s: Session, payment: Payment):
    rows = s.scalars(select(PaymentAllocation).where(
        PaymentAllocation.payment_id == payment.id, PaymentAllocation.released_at.is_(None)))
    return [(a.order_id, a.amount_minor) for a in rows]


def apply_credit(s: Session, client_id: int) -> int:
    """Переплата автоматически закрывает открытые долги клиента (сначала старые)."""
    credit = balance.client_credit(s, client_id)
    if credit <= 0:
        return 0
    orders = balance.open_orders(s, client_id)
    if not orders:
        return 0
    pays = s.scalars(select(Payment).where(
        Payment.client_id == client_id, Payment.kind == "payment"
    ).order_by(Payment.created_at, Payment.id)).all()
    pool = [[p, balance.payment_remaining(s, p)] for p in pays]
    pool = [x for x in pool if x[1] > 0]
    applied = 0
    for order, debt in orders:
        need = min(debt, credit - applied)
        while need > 0 and pool:
            p, rem = pool[0]
            take = min(rem, need)
            s.add(PaymentAllocation(payment_id=p.id, order_id=order.id, amount_minor=take))
            pool[0][1] -= take
            need -= take
            applied += take
            if pool[0][1] == 0:
                pool.pop(0)
        if applied >= credit:
            break
    s.flush()
    return applied


def accept_payment(s: Session, *, client_id: int, amount_minor: int, key: str,
                   order_ids=None, method: str = "cash", note=None) -> PaymentResult:
    existing = s.scalar(select(Payment).where(Payment.idempotency_key == key))
    if existing:  # повторный запрос — ничего не меняем
        return PaymentResult(existing, _allocs_of(s, existing),
                             balance.client_credit(s, client_id), True)
    if amount_minor <= 0:
        raise ServiceError("Сумма должна быть больше нуля.")
    if not s.get(Client, client_id):
        raise ServiceError("Клиент не найден.")

    if order_ids:
        targets = []
        for oid in dict.fromkeys(order_ids):
            o = s.get(Order, oid)
            if not o or o.client_id != client_id or o.status != "active":
                raise ServiceError(f"Заказ №{oid} недоступен для оплаты.")
            d = balance.order_debt(s, o)
            if d <= 0:
                raise ServiceError(f"Заказ №{oid} уже оплачен.")
            targets.append((o, d))
    else:
        targets = balance.open_orders(s, client_id)

    payment = Payment(client_id=client_id, kind="payment", amount_minor=amount_minor,
                      method=method, note=note, idempotency_key=key)
    s.add(payment)
    s.flush()
    remaining = amount_minor
    for order, debt in targets:
        if remaining <= 0:
            break
        part = min(debt, remaining)
        s.add(PaymentAllocation(payment_id=payment.id, order_id=order.id, amount_minor=part))
        remaining -= part
    s.flush()
    apply_credit(s, client_id)  # лишнее — на другие долги, иначе остаётся переплатой
    return PaymentResult(payment, _allocs_of(s, payment),
                         balance.client_credit(s, client_id), False)


def refund_credit(s: Session, *, client_id: int, amount_minor: int, key: str, note=None) -> Payment:
    existing = s.scalar(select(Payment).where(Payment.idempotency_key == key))
    if existing:
        return existing
    credit = balance.client_credit(s, client_id)
    if amount_minor <= 0 or amount_minor > credit:
        raise ServiceError("Сумма возврата должна быть больше нуля и не больше переплаты.")
    p = Payment(client_id=client_id, kind="refund", amount_minor=amount_minor,
                note=note, idempotency_key=key)
    s.add(p)
    s.flush()
    return p


def write_off(s: Session, *, order_id: int, amount_minor: int, reason, key: str) -> Adjustment:
    existing = s.scalar(select(Adjustment).where(Adjustment.idempotency_key == key))
    if existing:
        return existing
    o = s.get(Order, order_id)
    if not o or o.status != "active":
        raise ServiceError("Заказ недоступен.")
    debt = balance.order_debt(s, o)
    if amount_minor <= 0 or amount_minor > debt:
        raise ServiceError("Сумма списания должна быть больше нуля и не больше долга.")
    a = Adjustment(order_id=o.id, client_id=o.client_id, amount_minor=amount_minor,
                   reason=reason, idempotency_key=key)
    s.add(a)
    s.flush()
    return a
