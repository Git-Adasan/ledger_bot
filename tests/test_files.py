"""Двусторонняя печать (страницы → листы), файлы заказов, чеки и очистка."""
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from ledger_bot.db import session as dbs
from ledger_bot.db.models import Attachment, Order, Payment
from ledger_bot.db.session import init_db, session_scope
from ledger_bot.errors import ServiceError
from ledger_bot.main import _purge_files
from ledger_bot.services import attachments, backup, catalog, clients as client_svc, orders, payments
from ledger_bot.timeutil import now
from .conftest import svc
from .test_bot_flow import Harness, OWNER, run


# ---------- двусторонняя печать ----------
@pytest.mark.parametrize("pages, sheets", [(1, 1), (2, 1), (3, 2), (10, 5), (11, 6)])
def test_duplex_pages_to_sheets(db, pages, sheets):
    with session_scope() as s:
        sv = svc(s, "Двухсторонняя печать")
        line = orders.build_lines(s, [{"sid": sv.id, "vid": None, "qty": pages * 1000}])[0]
        assert line.qty_milli == sheets * 1000 and line.total_minor == sheets * 5500
        assert f"({pages} стр.)" in line.name


def test_duplex_rejects_fraction_and_single_side_untouched(db):
    with session_scope() as s:
        duplex, single = svc(s, "Двухсторонняя печать"), svc(s, "Односторонняя печать")
        with pytest.raises(ServiceError):
            orders.build_lines(s, [{"sid": duplex.id, "vid": None, "qty": 1500}])
        line = orders.build_lines(s, [{"sid": single.id, "vid": None, "qty": 7000}])[0]
        assert line.qty_milli == 7000 and line.total_minor == 7 * 3500 and "стр." not in line.name


def test_duplex_switch_in_catalog_and_fixed_ignores_it(db):
    with session_scope() as s:
        single = svc(s, "Односторонняя печать")
        catalog.update_service(s, single.id, pages_per_sheet=2)
        assert orders.build_lines(s, [{"sid": single.id, "vid": None, "qty": 3000}])[0].total_minor == 2 * 3500
        catalog.update_service(s, single.id, pages_per_sheet=1)
        assert orders.build_lines(s, [{"sid": single.id, "vid": None, "qty": 3000}])[0].total_minor == 3 * 3500
        with pytest.raises(ServiceError):
            catalog.update_service(s, single.id, pages_per_sheet=9)
        cat = catalog.list_categories(s)[0]
        fixed = catalog.create_service(s, "Скан", cat.id, "fixed", "усл.", 50000, pages_per_sheet=2)
        assert orders.build_lines(s, [{"sid": fixed.id, "vid": None, "qty": 1000}])[0].total_minor == 50000


def test_duplex_via_bot_and_toggle_buttons(db):
    async def scenario():
        h = Harness()
        await h.say("Калькулятор")
        await h.tap("oi:c:1")
        await h.tap("oi:s:2")                  # Двухсторонняя печать
        prompt = h.last_text()
        await h.say("3")                       # 3 страницы → 2 листа
        res = h.last_text()
        await h.say("Каталог услуг")
        await h.tap("cg:pp:2")                 # выключили двусторонний режим
        card = h.last_text()
        return prompt, res, card
    prompt, res, card = run(scenario())
    assert "Сколько страниц" in prompt and "Итого: 110 ₸" in res
    assert "стр. на лист" not in card
    with session_scope() as s:
        assert svc(s, "Двухсторонняя печать").pages_per_sheet == 1


# ---------- миграция старой базы и старых копий ----------
def _make_old_db(path):
    """База в старом формате: без pages_per_sheet и без таблицы attachments."""
    init_db(path)
    with session_scope() as s:
        catalog.seed_defaults(s)
        cid = client_svc.create_client(s, "Старый клиент").id
        orders.create_order(s, items=[{"sid": svc(s, "Конспект").id, "vid": None, "qty": 1000}],
                            client_id=cid, key="old1")
    dbs.dispose_engine()
    con = sqlite3.connect(path)
    for t in ("services", "service_variants"):
        con.execute(f"ALTER TABLE {t} DROP COLUMN pages_per_sheet")
    con.execute("DROP TABLE attachments")
    con.commit()
    con.close()


def test_old_database_is_migrated_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    _make_old_db(path)
    init_db(path)                              # как при запуске новой версии бота
    with session_scope() as s:
        names = {x.name: x for x in catalog.list_services(s)}
        assert names["Двухсторонняя печать"].pages_per_sheet == 2
        assert names["Односторонняя печать"].pages_per_sheet == 1
        album = names["Альбом по гистологии"]
        assert [v.pages_per_sheet for v in album.variants] == [1, 2]
        assert s.query(Order).count() == 1 and s.query(Attachment).count() == 0   # данные целы
    init_db(path)                              # повторный запуск ничего не ломает
    with session_scope() as s:
        assert catalog.list_services(s)


def test_old_backup_can_be_restored(tmp_path):
    old = tmp_path / "old.db"
    _make_old_db(str(old))
    backup.validate_db_file(old)               # копия без новой таблицы принимается
    target = str(tmp_path / "live.db")
    init_db(target)
    backup.restore_backup(old, target, tmp_path / "bk")
    init_db(target)
    with session_scope() as s:
        assert s.query(Order).count() == 1
        assert {x.name: x for x in catalog.list_services(s)}["Двухсторонняя печать"].pages_per_sheet == 2


# ---------- файлы и чеки: сервис ----------
def _order_with_payment(s, key="a"):
    cid = client_svc.create_client(s, "Мадина").id
    o, _ = orders.create_order(s, items=[{"sid": svc(s, "Конспект").id, "vid": None, "qty": 1000}],
                               client_id=cid, key=key)
    r = payments.accept_payment(s, client_id=cid, amount_minor=20000, key="p" + key)
    return o, r.payment


def test_attach_file_and_receipt_with_dedupe(db):
    with session_scope() as s:
        o, p = _order_with_payment(s)
        f = dict(media="document", file_id="F1", unique_id="U1", file_name="lecture.pdf", size=10)
        a1, new1 = attachments.add(s, order_id=o.id, **f)
        a2, new2 = attachments.add(s, order_id=o.id, **f)
        assert new1 and not new2 and a1.id == a2.id and a1.kind == "file"
        r, _ = attachments.add(s, payment_id=p.id, media="photo", file_id="F2", unique_id="U2")
        assert r.kind == "receipt"
        (pay, amount, receipts), = attachments.order_payments(s, o.id)
        assert pay.id == p.id and amount == 20000 and [x.id for x in receipts] == [r.id]
        with pytest.raises(ServiceError):
            attachments.add(s, media="photo", file_id="x")                      # некуда прикреплять
        with pytest.raises(ServiceError):
            attachments.add(s, order_id=o.id, payment_id=p.id, media="photo", file_id="x")
        with pytest.raises(ServiceError):
            attachments.add(s, order_id=999, media="photo", file_id="x")


def test_purge_removes_link_after_retention_but_keeps_record(db):
    with session_scope() as s:
        o, p = _order_with_payment(s)
        old, _ = attachments.add(s, order_id=o.id, media="document", file_id="OLD", unique_id="u1", file_name="old.pdf")
        fresh, _ = attachments.add(s, payment_id=p.id, media="photo", file_id="NEW", unique_id="u2")
        old.created_at = now() - timedelta(days=15)
        fresh.created_at = now() - timedelta(days=13)
        oid, pid, old_id, fresh_id = o.id, p.id, old.id, fresh.id
    assert _purge_files(14) == 1
    assert _purge_files(14) == 0                                  # повтор ничего не меняет
    with session_scope() as s:
        a = s.get(Attachment, old_id)
        assert a.file_id is None and a.purged_at is not None and a.file_name == "old.pdf" and a.order_id == oid
        b = s.get(Attachment, fresh_id)
        assert b.file_id == "NEW" and b.purged_at is None and b.payment_id == pid


# ---------- через бота ----------
def test_order_with_files_receipt_and_show(db):
    with session_scope() as s:
        cid = client_svc.create_client(s, "Айгерим").id

    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        await h.tap(f"oc:{cid}")
        await h.tap("oi:c:1")
        await h.tap("oi:s:1")
        await h.say("10")
        await h.tap("od:done")
        await h.tap("fl:draft")
        await h.send_file("gistology.pdf")
        await h.send_file("gistology.pdf")     # дубль в одном черновике не добавляется
        await h.send_file(photo=True)
        await h.tap("fl:done")
        assert "Файлов к заказу: 2" in h.last_text()
        await h.tap("od:ok")
        await h.say("Приём оплаты")
        await h.tap(f"pc:{cid}")
        await h.tap("po:next")
        await h.say("350")
        await h.tap("pk:ok")
        assert "Можно сохранить чек" in h.last_text()
        with session_scope() as s:
            pid = s.scalar(select(Payment.id))
        await h.tap(f"fl:rc:{pid}")
        await h.send_file("kaspi_check.pdf")
        await h.tap("fl:fin")
        with session_scope() as s:
            oid = s.scalar(select(Order.id))
        await h.tap(f"cl:od:{oid}")
        card = h.last_text()
        before = len(h.session.calls)
        await h.tap(f"fl:show:{oid}")
        sent = [type(c).__name__ for c in h.session.calls[before:]]
        return card, sent
    card, sent = run(scenario())
    assert "gistology.pdf — хранится до" in card and "фото — хранится до" in card
    assert "(чек сохранён)" in card
    assert sent.count("SendDocument") == 2 and sent.count("SendPhoto") == 1   # 2 файла + чек, 1 фото
    with session_scope() as s:
        assert s.query(Attachment).filter(Attachment.order_id.is_not(None)).count() == 2
        assert s.query(Attachment).filter(Attachment.payment_id.is_not(None)).count() == 1


def test_show_after_purge_says_file_deleted_but_record_stays(db):
    with session_scope() as s:
        o, _ = _order_with_payment(s)
        a, _ = attachments.add(s, order_id=o.id, media="document", file_id="X", unique_id="u", file_name="old.pdf")
        a.created_at = now() - timedelta(days=20)
        oid = o.id
    _purge_files(14)

    async def scenario():
        h = Harness()
        await h.tap(f"cl:od:{oid}")
        card = h.last_text()
        before = len(h.session.calls)
        await h.tap(f"fl:show:{oid}")
        return card, [type(c).__name__ for c in h.session.calls[before:]]
    card, sent = run(scenario())
    assert "old.pdf — файл удалён, запись осталась" in card and "SendDocument" not in sent


def test_stray_file_to_new_order_existing_order_and_receipt(db):
    with session_scope() as s:
        o, p = _order_with_payment(s)
        oid, pid = o.id, p.id

    async def scenario():
        h = Harness()
        await h.send_file("from_client.docx")           # файл без контекста
        assert "Куда сохранить" in h.last_text()
        await h.tap("fl:lo")
        await h.tap(f"fl:to:o:{oid}")
        await h.send_file("kaspi.pdf")
        await h.tap("fl:lp")
        await h.tap(f"fl:to:p:{pid}")
        await h.send_file("third.docx")                 # новый заказ с файлом
        await h.tap("fl:new")
        await h.tap("oc:none")
        await h.tap("oi:c:1")
        await h.tap("oi:s:1")
        await h.say("4")
        await h.tap("od:done")
        await h.tap("od:ok")
    run(scenario())
    with session_scope() as s:
        rows = s.scalars(select(Attachment).order_by(Attachment.id)).all()
        assert [(r.file_name, r.kind) for r in rows] == [
            ("from_client.docx", "file"), ("kaspi.pdf", "receipt"), ("third.docx", "file")]
        assert rows[0].order_id == oid and rows[1].payment_id == pid and rows[2].order_id != oid


def test_file_in_other_state_is_not_hijacked_and_restore_still_works(db, tmp_path):
    from ledger_bot.bot.handlers import settings
    settings.CFG.update(db=db, backups=tmp_path / "bk")

    async def scenario():
        h = Harness()
        await h.say("Настройки")
        await h.tap("bk:restore")
        await h.send_file("not_a_db.txt")               # ждёт файл копии: обрабатывает restore, а не files
        return h.last_text()
    run(scenario())
    with session_scope() as s:
        assert s.query(Attachment).count() == 0
