"""Файлы заказов и чеки оплат.

Файл НЕ скачивается на сервер: Telegram уже хранит его и позволяет отправить снова по file_id.
Поэтому диск сервера не заполняется. Через RETENTION_DAYS дней ссылка стирается, а запись
(имя файла, дата, к какому заказу/платежу относилась) остаётся как доказательство.
"""
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db.models import Attachment, Order, Payment, PaymentAllocation
from ..errors import ServiceError
from ..timeutil import now

RETENTION_DAYS = 14  # main.py подставляет FILE_RETENTION_DAYS


def add(s: Session, *, media: str, file_id: str, unique_id=None, file_name=None, size=None,
        order_id=None, payment_id=None) -> tuple[Attachment, bool]:
    """Прикрепляет файл к заказу ИЛИ к платежу. Тот же файл повторно не добавляется."""
    if (order_id is None) == (payment_id is None):
        raise ServiceError("Файл нужно прикрепить либо к заказу, либо к платежу.")
    if order_id is not None and not s.get(Order, order_id):
        raise ServiceError("Заказ не найден.")
    if payment_id is not None:
        p = s.get(Payment, payment_id)
        if not p or p.kind != "payment":
            raise ServiceError("Платёж не найден.")
    if unique_id:
        dup = s.scalar(select(Attachment).where(
            Attachment.unique_id == unique_id, Attachment.order_id == order_id,
            Attachment.payment_id == payment_id, Attachment.purged_at.is_(None)))
        if dup:
            return dup, False
    a = Attachment(order_id=order_id, payment_id=payment_id, media=media, file_id=file_id,
                   unique_id=unique_id, file_name=(file_name or None) and file_name[:200],
                   size=size)
    s.add(a)
    s.flush()
    return a, True


def for_order(s: Session, order_id: int) -> list[Attachment]:
    return list(s.scalars(select(Attachment).where(
        Attachment.order_id == order_id).order_by(Attachment.id)))


def for_payment(s: Session, payment_id: int) -> list[Attachment]:
    return list(s.scalars(select(Attachment).where(
        Attachment.payment_id == payment_id).order_by(Attachment.id)))


def order_payments(s: Session, order_id: int) -> list[tuple[Payment, int, list[Attachment]]]:
    """Платежи, которые закрывали этот заказ: (платёж, сколько пошло на заказ, чеки)."""
    rows = s.execute(
        select(Payment, PaymentAllocation.amount_minor)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .where(PaymentAllocation.order_id == order_id, PaymentAllocation.released_at.is_(None))
        .order_by(Payment.created_at, Payment.id)).all()
    return [(p, amt, for_payment(s, p.id)) for p, amt in rows]


def expires_on(a: Attachment, days: int | None = None):
    return (a.created_at + timedelta(days=days or RETENTION_DAYS)).date()


def purge_expired(s: Session, days: int | None = None) -> int:
    """Стирает ссылки на файлы старше N дней. Запись (имя, дата, привязка) остаётся."""
    limit = now() - timedelta(days=days or RETENTION_DAYS)
    res = s.execute(update(Attachment).where(
        Attachment.file_id.is_not(None), Attachment.created_at < limit
    ).values(file_id=None, purged_at=now()))
    return res.rowcount or 0
