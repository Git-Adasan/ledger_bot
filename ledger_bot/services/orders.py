from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Client, Order, OrderItem, Payment, PaymentAllocation
from ..errors import ServiceError
from ..timeutil import now
from . import balance, catalog, payments
from .calc import line_total, validate_qty


@dataclass
class Line:
    service_id: int
    variant_id: int | None
    name: str
    qty_milli: int
    unit: str
    price_minor: int
    total_minor: int


def pages_to_sheets(sv, variant, qty_milli: int) -> tuple[int, str]:
    """Что считать в заказе: (количество для расчёта, пояснение к названию).

    Обычные услуги: введённое количество как есть.
    Двусторонняя печать (2 страницы на лист): вводят СТРАНИЦЫ, листов = страницы / 2 с округлением
    вверх (нечётная последняя страница всё равно занимает целый лист). Цена умножается на листы.
    """
    pps = catalog.pages_per_sheet(sv, variant)
    if pps <= 1:
        validate_qty(sv.calc_type, qty_milli)
        return qty_milli, ""
    if qty_milli % 1000:
        raise ServiceError("Страниц должно быть целое число.")
    pages = qty_milli // 1000
    sheets = -(-pages // pps)
    return sheets * 1000, f" ({pages} стр.)"


def build_lines(s: Session, items: list[dict]) -> list[Line]:
    """items: [{'sid':…, 'vid':…|None, 'qty': milli}] → строки с текущими ценами каталога."""
    if not items:
        raise ServiceError("В заказе нет позиций.")
    lines = []
    for it in items:
        sv = catalog.get_service(s, it["sid"])
        if sv.is_archived or not sv.is_active:
            raise ServiceError(f"Услуга «{sv.name}» сейчас недоступна.")
        variants = catalog.active_variants(sv)
        variant = None
        if it.get("vid"):
            variant = next((v for v in variants if v.id == it["vid"]), None)
            if not variant:
                raise ServiceError(f"Вариант услуги «{sv.name}» недоступен.")
        elif variants:
            raise ServiceError(f"Для «{sv.name}» нужно выбрать вариант.")
        qty, note = pages_to_sheets(sv, variant, it["qty"])
        price = variant.price_minor if variant else sv.price_minor
        name = (f"{sv.name} — {variant.name}" if variant else sv.name) + note
        lines.append(Line(sv.id, variant.id if variant else None, name, qty,
                          sv.unit, price, line_total(price, qty)))
    return lines


def create_order(s: Session, *, items, client_id, key: str, note=None) -> tuple[Order, bool]:
    existing = s.scalar(select(Order).where(Order.idempotency_key == key))
    if existing:
        return existing, False
    if client_id is not None:
        c = s.get(Client, client_id)
        if not c or c.is_archived:
            raise ServiceError("Клиент не найден или в архиве.")
    lines = build_lines(s, items)
    order = Order(client_id=client_id, total_minor=sum(l.total_minor for l in lines),
                  note=note, idempotency_key=key)
    order.items = [OrderItem(service_id=l.service_id, variant_id=l.variant_id, name=l.name,
                             qty_milli=l.qty_milli, unit=l.unit, price_minor=l.price_minor,
                             total_minor=l.total_minor) for l in lines]
    s.add(order)
    s.flush()
    if client_id is None:
        # личная запись без клиента: деньги считаются полученными сразу
        if order.total_minor > 0:
            p = Payment(client_id=None, kind="payment", amount_minor=order.total_minor,
                        method="cash", note=f"Личная запись, заказ №{order.id}",
                        idempotency_key=f"direct-{key}")
            s.add(p)
            s.flush()
            s.add(PaymentAllocation(payment_id=p.id, order_id=order.id,
                                    amount_minor=order.total_minor))
    else:
        payments.apply_credit(s, client_id)  # предоплата закрывает новый заказ
    s.flush()
    return order, True


def cancel_order(s: Session, order_id: int, reason: str = "") -> tuple[Order, bool]:
    order = s.get(Order, order_id)
    if not order:
        raise ServiceError("Заказ не найден.")
    if order.status == "cancelled":
        return order, False
    allocs = list(s.scalars(select(PaymentAllocation).where(
        PaymentAllocation.order_id == order.id, PaymentAllocation.released_at.is_(None))))
    released = 0
    for a in allocs:
        a.released_at = now()
        released += a.amount_minor
    order.status = "cancelled"
    order.cancelled_at = now()
    order.cancel_reason = reason or None
    if order.client_id is None and released > 0:
        s.add(Payment(client_id=None, kind="refund", amount_minor=released,
                      note=f"Отмена заказа №{order.id}", idempotency_key=f"cancel-refund-{order.id}"))
    s.flush()
    if order.client_id is not None:
        payments.apply_credit(s, order.client_id)  # освободившиеся деньги → переплата/другие долги
    return order, True


def order_totals(s: Session, order: Order) -> dict:
    paid = balance.allocated_to_order(s, order.id)
    written = balance.adjusted_for_order(s, order.id)
    return {"total": order.total_minor, "paid": paid, "written_off": written,
            "debt": balance.order_debt(s, order)}
