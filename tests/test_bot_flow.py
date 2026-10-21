"""Сквозной прогон Telegram-обработчиков через диспетчер с поддельным Bot API."""
import asyncio
import time
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Chat, Message, Update, User

from ledger_bot.db.models import Client, Order, Payment, Transaction
from ledger_bot.db.session import session_scope
from ledger_bot.bot.handlers import catalog, clients, finance, menu, orders, payments, settings
from ledger_bot.main import build_dispatcher
from ledger_bot.services import balance, clients as client_svc

OWNER = 111


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(message_id=len(self.calls), date=datetime.now(),
                       chat=Chat(id=OWNER, type="private"), text=getattr(method, "text", "ok"))

    def texts(self):
        return [getattr(c, "text", None) or getattr(c, "caption", None) or getattr(c, "show_alert", None) and c.text
                for c in self.calls]


class Harness:
    def __init__(self, owner_ids=(OWNER,)):
        self.session = FakeSession()
        self.bot = Bot("123:ABC", session=self.session)
        for r in (menu, orders, clients, payments, catalog, finance, settings):
            r.router._parent_router = None  # роутеры — модульные синглтоны; в тестах строим диспетчер заново
        self.dp = build_dispatcher(owner_ids)
        self.n = 0

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": "x"}

    def _msg(self, uid, **extra):
        self.n += 1
        return {"message_id": self.n, "date": int(time.time()),
                "chat": {"id": uid, "type": "private"}, "from": self._user(uid), **extra}

    async def _feed(self, data):
        self.n += 1
        upd = Update.model_validate({"update_id": self.n, **data}, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, upd)

    async def say(self, text, uid=OWNER):
        await self._feed({"message": self._msg(uid, text=text)})

    async def tap(self, data, uid=OWNER):
        self.n += 1
        await self._feed({"callback_query": {
            "id": str(self.n), "from": self._user(uid), "chat_instance": "c", "data": data,
            "message": self._msg(uid, text="m")}})

    def last_text(self):
        return [c for c in self.session.calls if not isinstance(c, AnswerCallbackQuery) and getattr(c, "text", None)][-1].text


def run(coro):
    return asyncio.run(coro)


def test_order_without_client_and_stats(db):
    async def scenario():
        h = Harness()
        await h.say("/start")
        await h.say("Создать заказ")
        await h.tap("oc:none")
        await h.tap("oi:c:1")
        await h.tap("oi:s:1")
        await h.say("abc")                     # ошибка ввода, состояние сохраняется
        assert "количество" in h.last_text().lower()
        await h.say("10")
        await h.tap("od:done")
        await h.tap("od:ok")
        await h.tap("od:ok")                   # двойное нажатие — второй заказ не создаётся
        await h.say("Финансовая статистика")
        await h.tap("st:all")
        return h.last_text()
    text = run(scenario())
    with session_scope() as s:
        orders = s.query(Order).all()
        assert len(orders) == 1 and orders[0].total_minor == 35000 and orders[0].client_id is None
    assert "Получено от клиентов: 350 ₸" in text and "не чистая прибыль" in text


def test_client_order_partial_payment_no_double_apply(db):
    with session_scope() as s:
        cid = client_svc.create_client(s, "Айгерим").id

    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        await h.tap(f"oc:{cid}")
        await h.tap("oi:c:2")
        await h.tap("oi:s:3")                  # Конспект, 200 ₸/экз
        await h.say("3")
        await h.tap("od:done")
        await h.tap("od:ok")
        await h.say("Приём оплаты")
        await h.tap(f"pc:{cid}")
        await h.tap("po:next")
        await h.say("250")
        await h.tap("pk:ok")
        await h.tap("pk:ok")                   # повтор — не должен создать второй платёж
        await h.say("Должники")
        return h.last_text()
    text = run(scenario())
    with session_scope() as s:
        assert s.query(Payment).filter(Payment.client_id == cid).count() == 1
        assert balance.client_debt(s, cid) == 35000
    assert "350 ₸" in text


def test_album_variant_and_calculator(db):
    async def scenario():
        h = Harness()
        await h.say("Калькулятор")
        await h.tap("oi:c:3")
        await h.tap("oi:s:4")
        await h.tap("oi:v:4:2")                # двухсторонняя, 55 ₸
        await h.say("2")
        return h.last_text()
    text = run(scenario())
    assert "Итого: 110 ₸" in text
    with session_scope() as s:
        assert s.query(Order).count() == 0     # калькулятор ничего не сохраняет


def test_catalog_management_via_bot(db):
    async def scenario():
        h = Harness()
        await h.say("Каталог услуг")
        await h.tap("cg:new")
        await h.say("Сканирование")
        await h.tap("cg:nc:1")
        await h.tap("cg:nt:fixed")
        await h.say("500")
        await h.tap("cg:ed:price:1")           # изменить цену односторонней печати
        await h.say("40")
        await h.tap("cg:tg:1")                 # отключить
        await h.tap("cg:ar:2")                 # архивировать двухстороннюю
        await h.tap("cg:rs:2")                 # восстановить
        return h
    run(scenario())
    with session_scope() as s:
        from ledger_bot.services import catalog
        names = {x.name: x for x in catalog.list_services(s)}
        assert names["Сканирование"].price_minor == 50000 and names["Сканирование"].calc_type == "fixed"
        assert names["Односторонняя печать"].price_minor == 4000 and not names["Односторонняя печать"].is_active
        assert not names["Двухсторонняя печать"].is_archived


def test_expense_flow_and_menu_interrupts_input(db):
    async def scenario():
        h = Harness()
        await h.say("Доходы и расходы")
        await h.tap("fi:expense")
        await h.say("1 200")
        await h.tap("fd:today")
        await h.tap("fc:0")
        await h.tap("fs:desc")
        await h.tap("fs:note")
        await h.say("Создать заказ")           # меню прерывает ввод в любом состоянии
        await h.tap("fi:expense")              # состояние сброшено → ничего не ломается
    run(scenario())
    with session_scope() as s:
        t = s.query(Transaction).one()
        assert t.kind == "expense" and t.amount_minor == 120000 and t.category == "Бумага"


def test_stranger_is_ignored(db):
    async def scenario():
        h = Harness(owner_ids=(OWNER,))
        await h.say("/start", uid=999)
        await h.say("Создать заказ", uid=999)
        return h.session.calls
    assert run(scenario()) == []


def test_export_and_backup_buttons(db, tmp_path):
    from ledger_bot.bot.handlers import settings
    settings.CFG.update(db=db, backups=tmp_path / "bk")

    async def scenario():
        h = Harness()
        await h.say("Экспорт")
        await h.say("Настройки")
        await h.tap("bk:new")
        return [type(c).__name__ for c in h.session.calls]
    assert run(scenario()).count("SendDocument") == 2


def test_greeting_shows_debts(db):
    from ledger_bot.services import orders as order_svc
    from .conftest import svc

    async def start():
        h = Harness()
        await h.say("/start")
        return h.last_text()
    assert "Долгов сейчас нет." in run(start())
    with session_scope() as s:
        cid = client_svc.create_client(s, "Данияр").id
        order_svc.create_order(s, items=[{"sid": svc(s, "Конспект").id, "vid": None, "qty": 1000}],
                               client_id=cid, key="g")
    text = run(start())
    assert text.startswith("Здравствуйте!") and "Вам должны: 200 ₸ (человек: 1)" in text
