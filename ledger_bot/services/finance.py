from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Transaction
from ..errors import ServiceError
from ..timeutil import now

DEFAULT_CATEGORIES = {
    "income": ["Продажа", "Прочее"],
    "expense": ["Бумага", "Краска / картридж", "Техника", "Аренда", "Прочее"],
}


def add_transaction(s: Session, *, kind: str, amount_minor: int, day: date, category: str,
                    description=None, note=None, key: str) -> tuple[Transaction, bool]:
    existing = s.scalar(select(Transaction).where(Transaction.idempotency_key == key))
    if existing:
        return existing, False
    if kind not in ("income", "expense"):
        raise ServiceError("Неизвестный тип операции.")
    if amount_minor <= 0:
        raise ServiceError("Сумма должна быть больше нуля.")
    category = (category or "").strip()
    if not category or len(category) > 80:
        raise ServiceError("Категория: от 1 до 80 символов.")
    t = Transaction(kind=kind, amount_minor=amount_minor, date=day, category=category,
                    description=(description or "").strip() or None,
                    note=(note or "").strip() or None, idempotency_key=key)
    s.add(t)
    s.flush()
    return t, True


def void_transaction(s: Session, transaction_id: int) -> Transaction:
    t = s.get(Transaction, transaction_id)
    if not t:
        raise ServiceError("Операция не найдена.")
    if t.voided_at is None:
        t.voided_at = now()
    return t


def recent(s: Session, limit: int = 10) -> list[Transaction]:
    return list(s.scalars(select(Transaction).order_by(
        Transaction.date.desc(), Transaction.id.desc()).limit(limit)))


def known_categories(s: Session, kind: str) -> list[str]:
    used = list(s.scalars(select(Transaction.category).where(Transaction.kind == kind).distinct()))
    cats = list(DEFAULT_CATEGORIES[kind])
    for c in used:
        if c not in cats:
            cats.append(c)
    return cats[:12]
