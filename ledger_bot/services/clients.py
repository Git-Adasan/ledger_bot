from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Adjustment, Client, Order, Payment
from ..errors import ServiceError
from . import balance


def create_client(s: Session, name: str, phone=None, note=None) -> Client:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ServiceError("Имя: от 1 до 80 символов.")
    c = Client(name=name, phone=(phone or "").strip() or None, note=(note or "").strip() or None)
    s.add(c)
    s.flush()
    return c


def get_client(s: Session, client_id: int) -> Client:
    c = s.get(Client, client_id)
    if not c:
        raise ServiceError("Клиент не найден.")
    return c


def update_client(s: Session, client_id: int, field: str, value: str) -> Client:
    c = get_client(s, client_id)
    value = (value or "").strip()
    if field == "name":
        if not value or len(value) > 80:
            raise ServiceError("Имя: от 1 до 80 символов.")
        c.name = value
    elif field in ("phone", "note"):
        setattr(c, field, None if value in ("", "-") else value)
    else:
        raise ServiceError("Неизвестное поле.")
    return c


def set_archived(s: Session, client_id: int, archived: bool) -> Client:
    c = get_client(s, client_id)
    if archived and balance.client_debt(s, c.id) > 0:
        raise ServiceError("Нельзя архивировать клиента с долгом.")
    c.is_archived = archived
    return c


def search(s: Session, query: str, limit: int = 15) -> list[Client]:
    q = (query or "").strip().casefold()
    res = [c for c in s.scalars(select(Client).order_by(Client.name))
           if q in c.name.casefold() or q in (c.phone or "").casefold()]
    return res[:limit]


def recent_clients(s: Session, limit: int = 8) -> list[Client]:
    last = {}
    for cid, dt in s.execute(select(Order.client_id, Order.created_at).where(
            Order.client_id.is_not(None))):
        if cid not in last or dt > last[cid]:
            last[cid] = dt
    clients = list(s.scalars(select(Client).where(~Client.is_archived)))
    clients.sort(key=lambda c: (last.get(c.id) is None, -(last[c.id].timestamp()) if c.id in last else 0, c.name))
    return clients[:limit]


def debtors(s: Session) -> list[tuple[Client, int]]:
    res = []
    for c in s.scalars(select(Client).where(~Client.is_archived)):
        d = balance.client_debt(s, c.id)
        if d > 0:
            res.append((c, d))
    res.sort(key=lambda x: -x[1])
    return res


def debtors_with_since(s: Session) -> list[tuple[Client, int, "date"]]:
    """Должники: (клиент, долг, дата самого старого неоплаченного заказа). Старые долги — первыми."""
    res = []
    for c in s.scalars(select(Client).where(~Client.is_archived)):
        open_ = balance.open_orders(s, c.id)
        if open_:
            res.append((c, sum(d for _, d in open_), min(o.created_at for o, _ in open_).date()))
    res.sort(key=lambda x: (x[2], -x[1]))
    return res


def history(s: Session, client_id: int) -> list[dict]:
    """Все операции клиента от новых к старым: заказы, отмены, платежи, возвраты, списания."""
    ev = []
    for o in s.scalars(select(Order).where(Order.client_id == client_id)):
        ev.append({"dt": o.created_at, "text": f"Заказ №{o.id}", "amount": o.total_minor})
        if o.status == "cancelled":
            ev.append({"dt": o.cancelled_at, "text": f"Отмена заказа №{o.id}", "amount": -o.total_minor})
    for p in s.scalars(select(Payment).where(Payment.client_id == client_id)):
        if p.kind == "payment":
            ev.append({"dt": p.created_at, "text": "Оплата", "amount": p.amount_minor})
        else:
            ev.append({"dt": p.created_at, "text": "Возврат переплаты", "amount": -p.amount_minor})
    for a in s.scalars(select(Adjustment).where(Adjustment.client_id == client_id)):
        ev.append({"dt": a.created_at, "text": f"Списание долга по заказу №{a.order_id}",
                   "amount": -a.amount_minor})
    ev.sort(key=lambda e: e["dt"], reverse=True)
    return ev
