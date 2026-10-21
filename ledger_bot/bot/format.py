from sqlalchemy.orm import Session

from ..db.models import Order
from ..money import fmt_money, fmt_qty
from ..services import balance, clients as client_svc, orders as order_svc
from ..services.calc import CALC_TYPES


def fmt_dt(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def price_label(sv, price_minor=None) -> str:
    p = fmt_money(sv.price_minor if price_minor is None else price_minor)
    return p if sv.calc_type == "fixed" else f"{p} / {sv.unit}"


def line_text(i: int, name, qty_milli, unit, price_minor, total_minor, fixed=False) -> str:
    if fixed:
        return f"{i}. {name} — {fmt_money(total_minor)}"
    return f"{i}. {name} — {fmt_qty(qty_milli)} {unit} × {fmt_money(price_minor)} = {fmt_money(total_minor)}"


def order_text(s: Session, order: Order) -> str:
    client = client_svc.get_client(s, order.client_id).name if order.client_id else "без клиента (личная запись)"
    lines = [f"Заказ №{order.id} от {fmt_dt(order.created_at)}", f"Клиент: {client}"]
    for i, it in enumerate(order.items, 1):
        lines.append(line_text(i, it.name, it.qty_milli, it.unit, it.price_minor, it.total_minor))
    t = order_svc.order_totals(s, order)
    lines.append(f"\nИтого: {fmt_money(t['total'])}")
    if order.status == "cancelled":
        lines.append(f"ОТМЕНЁН {fmt_dt(order.cancelled_at)}")
    else:
        lines.append(f"Оплачено: {fmt_money(t['paid'])}")
        if t["written_off"]:
            lines.append(f"Списано: {fmt_money(t['written_off'])}")
        lines.append(f"Долг: {fmt_money(t['debt'])}")
    return "\n".join(lines)


def client_text(s: Session, client_id: int) -> str:
    c = client_svc.get_client(s, client_id)
    debt, credit = balance.client_debt(s, c.id), balance.client_credit(s, c.id)
    lines = [c.name + (" (в архиве)" if c.is_archived else "")]
    if c.phone:
        lines.append(f"Телефон: {c.phone}")
    if c.note:
        lines.append(f"Заметка: {c.note}")
    if debt:
        since = min(o.created_at for o, _ in balance.open_orders(s, c.id))
        lines.append(f"Долг: {fmt_money(debt)} (самый старый — с {since:%d.%m.%Y})")
    elif credit > 0:
        lines.append(f"Переплата: {fmt_money(credit)}")
    else:
        lines.append("Долгов нет")
    return "\n".join(lines)


def service_text(sv) -> str:
    st = "архив" if sv.is_archived else ("включена" if sv.is_active else "отключена")
    lines = [sv.name, f"Категория: {sv.category.name}",
             f"Расчёт: {CALC_TYPES[sv.calc_type].title}", f"Единица: {sv.unit}",
             f"Цена: {price_label(sv)}", f"Статус: {st}"]
    vs = [v for v in sv.variants]
    if vs:
        lines.append("Варианты:")
        for v in vs:
            lines.append(f"  {'✓' if v.is_active else '✗'} {v.name} — {price_label(sv, v.price_minor)}")
    return "\n".join(lines)
