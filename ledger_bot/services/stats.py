from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Adjustment, Client, Order, Payment, PaymentAllocation, Transaction
from ..money import fmt_money
from . import balance

PERIODS = {"today": "Сегодня", "week": "Неделя", "month": "Месяц",
           "prev": "Прошлый месяц", "all": "Всё время"}


def period_range(name: str, today: date):
    if name == "today":
        return today, today
    if name == "week":
        return today - timedelta(days=today.weekday()), today
    if name == "month":
        return today.replace(day=1), today
    if name == "prev":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return None, None


@dataclass
class Stats:
    start: date | None
    end: date | None
    orders_count: int
    orders_value: int
    payments: int
    refunds: int
    other_income: int
    expenses: int
    total_debt: int
    total_credit: int

    @property
    def net_received(self) -> int:
        return self.payments - self.refunds

    @property
    def money_movement(self) -> int:
        return self.net_received + self.other_income - self.expenses


def compute(s: Session, start: date | None, end: date | None) -> Stats:
    sdt = datetime.combine(start, datetime.min.time()) if start else None
    edt = datetime.combine(end + timedelta(days=1), datetime.min.time()) if end else None

    def dt_between(col, q):
        if sdt:
            q = q.where(col >= sdt)
        if edt:
            q = q.where(col < edt)
        return q

    cnt, val = s.execute(dt_between(Order.created_at, select(
        func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0)
    ).where(Order.status == "active"))).one()

    def pay_sum(kind):
        return s.execute(dt_between(Payment.created_at, select(
            func.coalesce(func.sum(Payment.amount_minor), 0)).where(Payment.kind == kind))).scalar()

    def tr_sum(kind):
        q = select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.kind == kind, Transaction.voided_at.is_(None))
        if start:
            q = q.where(Transaction.date >= start)
        if end:
            q = q.where(Transaction.date <= end)
        return s.execute(q).scalar()

    active_total = s.execute(select(func.coalesce(func.sum(Order.total_minor), 0)).where(
        Order.status == "active")).scalar()
    alloc = s.execute(select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0)).join(
        Order, Order.id == PaymentAllocation.order_id).where(
        Order.status == "active", PaymentAllocation.released_at.is_(None))).scalar()
    adj = s.execute(select(func.coalesce(func.sum(Adjustment.amount_minor), 0)).join(
        Order, Order.id == Adjustment.order_id).where(Order.status == "active")).scalar()
    credit = sum(max(balance.client_credit(s, cid), 0) for cid in s.scalars(select(Client.id)))
    return Stats(start, end, cnt, val, pay_sum("payment"), pay_sum("refund"),
                 tr_sum("income"), tr_sum("expense"), active_total - alloc - adj, credit)


def format_stats(st: Stats, title: str) -> str:
    rng = f" ({st.start:%d.%m.%Y}–{st.end:%d.%m.%Y})" if st.start and st.end else ""
    mv = st.money_movement
    sign = "+" if mv > 0 else ""
    return (
        f"Статистика: {title}{rng}\n\n"
        f"Создано заказов: {st.orders_count} на {fmt_money(st.orders_value)}\n"
        f"Получено от клиентов: {fmt_money(st.net_received)}"
        + (f" (возвраты: {fmt_money(st.refunds)})" if st.refunds else "") + "\n"
        f"Другие доходы: {fmt_money(st.other_income)}\n"
        f"Расходы: {fmt_money(st.expenses)}\n\n"
        f"Движение денег: {sign}{fmt_money(mv)}\n"
        "(полученное + другие доходы − расходы. Это не чистая прибыль: "
        "в расходах только то, что вы внесли сами.)\n\n"
        f"Долг клиентов сейчас: {fmt_money(st.total_debt)} (это ещё не полученные деньги)\n"
        f"Предоплаты клиентов: {fmt_money(st.total_credit)}"
    )
