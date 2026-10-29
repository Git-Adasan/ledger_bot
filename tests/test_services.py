import pytest
from sqlalchemy import select

from ledger_bot.db.models import Client, Order, Payment, PaymentAllocation
from ledger_bot.db.session import session_scope
from ledger_bot.errors import ServiceError
from ledger_bot.money import fmt_money, fmt_qty, parse_money, parse_qty
from ledger_bot.services import (backup, balance, catalog, clients, export, finance, orders,
                                 payments, stats)
from ledger_bot.services.calc import line_total
from ledger_bot.timeutil import today
from .conftest import svc


def mk_client(s, name="Алия"):
    return clients.create_client(s, name).id


def mk_order(s, cid, qty=10, name="Односторонняя печать", key=None):
    sv = svc(s, name)
    o, _ = orders.create_order(s, items=[{"sid": sv.id, "vid": None, "qty": qty * 1000}],
                               client_id=cid, key=key or f"k{s.query(Order).count()}")
    return o


def test_money():
    assert parse_money("1 500,50 ₸") == 150050
    assert fmt_money(150050) == "1 500,50 ₸" and fmt_money(3500) == "35 ₸"
    assert parse_qty("1,5") == 1500 and fmt_qty(1500) == "1.5" and fmt_qty(12000) == "12"
    for bad in ("abc", "0", "-5", "1.234", "nan"):
        with pytest.raises(ServiceError):
            parse_money(bad)


def test_line_total_rounding():
    assert line_total(3500, 10000) == 35000
    assert line_total(333, 1500) == 500  # 499.5 → 500


def test_seed_and_variants(db):
    with session_scope() as s:
        names = {x.name: x for x in catalog.list_services(s)}
        assert names["Односторонняя печать"].price_minor == 3500
        assert names["Двухсторонняя печать"].price_minor == 5500
        assert names["Конспект"].price_minor == 20000 and names["Конспект"].calc_type == "copy"
        album = names["Альбом по гистологии"]
        assert len(catalog.active_variants(album)) == 2
        with pytest.raises(ServiceError):  # вариант обязателен
            orders.build_lines(s, [{"sid": album.id, "vid": None, "qty": 5000}])
        v2 = album.variants[1]
        line = orders.build_lines(s, [{"sid": album.id, "vid": v2.id, "qty": 5000}])[0]
        # вводятся страницы: 5 стр. = 3 листа (нечётная страница занимает лист)
        assert line.total_minor == 3 * 5500 and "Двухсторонняя" in line.name and "(5 стр.)" in line.name
        assert line.qty_milli == 3000


def test_catalog_changes_do_not_touch_history(db):
    with session_scope() as s:
        o = mk_order(s, None, 10)
        oid = o.id
    with session_scope() as s:
        sv = svc(s, "Односторонняя печать")
        catalog.update_service(s, sv.id, name="Печать А4", price_minor=9900)
        catalog.archive_service(s, sv.id)
    with session_scope() as s:
        o = s.get(Order, oid)
        assert o.items[0].name == "Односторонняя печать"
        assert o.items[0].price_minor == 3500 and o.total_minor == 35000
        with pytest.raises(ServiceError):  # архивную услугу заказать нельзя
            orders.build_lines(s, [{"sid": sv.id, "vid": None, "qty": 1000}])
        catalog.restore_service(s, sv.id)
        assert orders.build_lines(s, [{"sid": sv.id, "vid": None, "qty": 1000}])[0].price_minor == 9900


def test_calc_types_validation(db):
    with session_scope() as s:
        cat = catalog.list_categories(s)[0]
        fixed = catalog.create_service(s, "Сканирование", cat.id, "fixed", "усл.", 50000)
        hour = catalog.create_service(s, "Набор текста", cat.id, "hour", "ч", 100000)
        with pytest.raises(ServiceError):
            orders.build_lines(s, [{"sid": fixed.id, "vid": None, "qty": 2000}])
        assert orders.build_lines(s, [{"sid": fixed.id, "vid": None, "qty": 1000}])[0].total_minor == 50000
        assert orders.build_lines(s, [{"sid": hour.id, "vid": None, "qty": 1500}])[0].total_minor == 150000
        sheet = svc(s, "Односторонняя печать")
        with pytest.raises(ServiceError):
            orders.build_lines(s, [{"sid": sheet.id, "vid": None, "qty": 1500}])


def test_multi_item_order_and_idempotency(db):
    with session_scope() as s:
        cid = mk_client(s)
        a, b = svc(s, "Конспект"), svc(s, "Двухсторонняя печать")
        items = [{"sid": a.id, "vid": None, "qty": 2000}, {"sid": b.id, "vid": None, "qty": 4000}]  # 4 стр. = 2 листа
        o1, c1 = orders.create_order(s, items=items, client_id=cid, key="same")
        o2, c2 = orders.create_order(s, items=items, client_id=cid, key="same")
        assert c1 and not c2 and o1.id == o2.id
        assert o1.total_minor == 2 * 20000 + 2 * 5500
        assert s.query(Order).count() == 1


def test_partial_multiple_payments_and_fifo(db):
    with session_scope() as s:
        cid = mk_client(s)
        o1, o2 = mk_order(s, cid, 10, key="a"), mk_order(s, cid, 20, key="b")  # 350 и 700
        r = payments.accept_payment(s, client_id=cid, amount_minor=20000, key="p1")
        assert r.allocations == [(o1.id, 20000)]
        assert balance.client_debt(s, cid) == 35000 - 20000 + 70000
        r = payments.accept_payment(s, client_id=cid, amount_minor=30000, key="p2")
        assert dict(r.allocations) == {o1.id: 15000, o2.id: 15000}
        assert balance.order_debt(s, o1) == 0 and balance.order_debt(s, o2) == 55000


def test_payment_idempotent(db):
    with session_scope() as s:
        cid = mk_client(s)
        mk_order(s, cid, 10)
        r1 = payments.accept_payment(s, client_id=cid, amount_minor=10000, key="dup")
        r2 = payments.accept_payment(s, client_id=cid, amount_minor=10000, key="dup")
        assert not r1.duplicate and r2.duplicate
        assert s.query(Payment).count() == 1
        assert balance.client_debt(s, cid) == 25000


def test_payment_selected_orders_and_multi_order(db):
    with session_scope() as s:
        cid = mk_client(s)
        o1, o2 = mk_order(s, cid, 10, key="a"), mk_order(s, cid, 20, key="b")
        r = payments.accept_payment(s, client_id=cid, amount_minor=70000, key="p", order_ids=[o2.id])
        assert r.allocations == [(o2.id, 70000)] and balance.order_debt(s, o1) == 35000
        with pytest.raises(ServiceError):
            payments.accept_payment(s, client_id=cid, amount_minor=100, key="q", order_ids=[o2.id])


def test_overpayment_credit_and_autoapply(db):
    with session_scope() as s:
        cid = mk_client(s)
        o1 = mk_order(s, cid, 10, key="a")  # 350
        r = payments.accept_payment(s, client_id=cid, amount_minor=50000, key="p")
        assert r.credit_after == 15000 and balance.client_debt(s, cid) == 0
        o2 = mk_order(s, cid, 10, key="b")  # 350: 150 закрывается предоплатой
        assert balance.order_debt(s, o2) == 20000 and balance.client_credit(s, cid) == 0
        # возврат переплаты: нельзя больше, чем есть
        payments.accept_payment(s, client_id=cid, amount_minor=30000, key="p2")
        assert balance.client_credit(s, cid) == 10000
        with pytest.raises(ServiceError):
            payments.refund_credit(s, client_id=cid, amount_minor=10001, key="r0")
        payments.refund_credit(s, client_id=cid, amount_minor=10000, key="r1")
        assert balance.client_credit(s, cid) == 0


def test_cancel_order_adjusts_debt_and_keeps_history(db):
    with session_scope() as s:
        cid = mk_client(s)
        o1, o2 = mk_order(s, cid, 10, key="a"), mk_order(s, cid, 10, key="b")
        payments.accept_payment(s, client_id=cid, amount_minor=35000, key="p", order_ids=[o1.id])
        orders.cancel_order(s, o1.id, "ошибка")
        _, again = orders.cancel_order(s, o1.id)
        assert not again
        # оплаченные 350 стали переплатой и сразу закрыли долг по o2
        assert balance.order_debt(s, o2) == 0 and balance.client_credit(s, cid) == 0
        assert s.query(Order).count() == 2  # заказ не удалён
        assert s.query(PaymentAllocation).filter(PaymentAllocation.released_at.is_not(None)).count() == 1
        texts = [e["text"] for e in clients.history(s, cid)]
        assert "Отмена заказа №%d" % o1.id in texts


def test_write_off(db):
    with session_scope() as s:
        cid = mk_client(s)
        o = mk_order(s, cid, 10)
        payments.write_off(s, order_id=o.id, amount_minor=5000, reason="скидка", key="w")
        payments.write_off(s, order_id=o.id, amount_minor=5000, reason="скидка", key="w")
        assert balance.order_debt(s, o) == 30000
        with pytest.raises(ServiceError):
            payments.write_off(s, order_id=o.id, amount_minor=99999999, reason="", key="w2")
        st = stats.compute(s, None, None)
        assert st.total_debt == 30000 and st.net_received == 0  # списание ≠ деньги


def test_personal_order_without_client(db):
    with session_scope() as s:
        o = mk_order(s, None, 10)
        assert balance.order_debt(s, o) == 0
        st = stats.compute(s, None, None)
        assert st.net_received == 35000 and st.total_debt == 0
        orders.cancel_order(s, o.id)
        st = stats.compute(s, None, None)
        assert st.net_received == 0 and st.orders_value == 0


def test_stats_separation(db):
    with session_scope() as s:
        cid = mk_client(s)
        mk_order(s, cid, 20, key="a")  # 700
        payments.accept_payment(s, client_id=cid, amount_minor=20000, key="p")
        finance.add_transaction(s, kind="income", amount_minor=10000, day=today(),
                                category="Прочее", key="i1")
        t, _ = finance.add_transaction(s, kind="expense", amount_minor=7000, day=today(),
                                       category="Бумага", key="e1")
        e2, _ = finance.add_transaction(s, kind="expense", amount_minor=999, day=today(),
                                        category="Бумага", key="e2")
        finance.void_transaction(s, e2.id)
        _, dup = finance.add_transaction(s, kind="expense", amount_minor=7000, day=today(),
                                         category="Бумага", key="e1")
        assert not dup
        st = stats.compute(s, *stats.period_range("month", today()))
        assert (st.orders_value, st.net_received, st.other_income, st.expenses) == (70000, 20000, 10000, 7000)
        assert st.total_debt == 50000 and st.money_movement == 23000
        text = stats.format_stats(st, "Месяц")
        assert "не чистая прибыль" in text and "Прибыль:" not in text


def test_debtors_and_search(db):
    with session_scope() as s:
        a, b = mk_client(s, "Айгерим"), mk_client(s, "Данияр")
        mk_order(s, a, 10, key="a")
        mk_order(s, b, 30, key="b")
        assert [c.name for c, _ in clients.debtors(s)] == ["Данияр", "Айгерим"]
        assert [c.name for c in clients.search(s, "айг")] == ["Айгерим"]
        with pytest.raises(ServiceError):
            clients.set_archived(s, a, True)


def test_backup_restore_and_export(db, tmp_path):
    with session_scope() as s:
        cid = mk_client(s, "До копии")
        mk_order(s, cid, 10)
    b = backup.create_backup(db, tmp_path / "bk")
    with session_scope() as s:
        clients.create_client(s, "После копии")
    backup.restore_backup(b, db, tmp_path / "bk")
    with session_scope() as s:
        names = {c.name for c in s.scalars(select(Client))}
        assert names == {"До копии"}
        data = export.export_zip(s)
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"orders.csv", "payments.csv", "transactions.csv", "clients.csv"} <= set(z.namelist())
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    with pytest.raises(ServiceError):
        backup.validate_db_file(bad)


def test_debtors_show_oldest_debt_date(db):
    from datetime import datetime
    with session_scope() as s:
        a, b = mk_client(s, "Новый долг"), mk_client(s, "Старый долг")
        oa, ob = mk_order(s, a, 10, key="a"), mk_order(s, b, 10, key="b")
        ob.created_at = datetime(2026, 9, 1, 12, 0)
        mk_order(s, b, 5, key="c")
        rows = clients.debtors_with_since(s)
        assert [r[0].name for r in rows] == ["Старый долг", "Новый долг"]
        assert rows[0][2].isoformat() == "2026-09-01" and rows[0][1] == 35000 + 17500
