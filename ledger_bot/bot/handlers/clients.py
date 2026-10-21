import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from ...db.models import Order
from ...db.session import session_scope
from ...money import fmt_money, parse_money
from ...services import balance, clients as client_svc, orders as order_svc, payments as pay_svc
from ..common import show
from ..format import client_text, fmt_dt, order_text
from ...timeutil import today
from ..keyboards import CANCEL, ikb

router = Router()


class ClientFlow(StatesGroup):
    search = State()
    new_name = State()
    new_phone = State()
    edit = State()
    writeoff = State()
    refund = State()


async def start_clients(message: Message, state: FSMContext):
    await message.answer("Клиенты", reply_markup=ikb([
        [("Список", "cl:list"), ("Найти", "cl:find")], [("Новый клиент", "cl:new")]]))


async def start_debtors(message: Message, state: FSMContext):
    with session_scope() as s:
        rows = [(c.id, c.name, d, since) for c, d, since in client_svc.debtors_with_since(s)]
    if not rows:
        await message.answer("Должников нет.")
        return
    total = sum(r[2] for r in rows)
    kb = [[(f"{n} — {fmt_money(d)} (с {since:%d.%m}, {(today() - since).days} дн.)", f"cl:c:{i}")]
          for i, n, d, since in rows]
    await message.answer(f"Должники: {len(rows)}, общий долг {fmt_money(total)}\n"
                         "Сверху те, кто должен дольше всех. Это ещё не полученные деньги.",
                         reply_markup=ikb(kb))


@router.callback_query(F.data == "cl:list")
async def list_clients(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = []
        for c in client_svc.recent_clients(s, 30):
            d = balance.client_debt(s, c.id)
            rows.append([(c.name + (f" — долг {fmt_money(d)}" if d else ""), f"cl:c:{c.id}")])
    await show(cb, "Клиенты (последние по заказам):" if rows else "Клиентов пока нет.", ikb(rows) if rows else None)


@router.callback_query(F.data == "cl:find")
async def find_prompt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ClientFlow.search)
    await show(cb, "Введите часть имени или телефона:", ikb([CANCEL]))


@router.message(ClientFlow.search, F.text)
async def find(message: Message, state: FSMContext):
    with session_scope() as s:
        found = [(c.id, f"{c.name} {c.phone or ''}".strip() + (" (архив)" if c.is_archived else ""))
                 for c in client_svc.search(s, message.text)]
    await state.clear()
    if not found:
        await message.answer("Никого не нашёл.")
        return
    await message.answer("Найдено:", reply_markup=ikb([[(n, f"cl:c:{i}")] for i, n in found]))


@router.callback_query(F.data == "cl:new")
async def new_prompt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ClientFlow.new_name)
    await show(cb, "Имя клиента:", ikb([CANCEL]))


@router.message(ClientFlow.new_name, F.text)
async def new_name(message: Message, state: FSMContext):
    if not message.text.strip() or len(message.text.strip()) > 80:
        await message.answer("Имя: от 1 до 80 символов.")
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(ClientFlow.new_phone)
    await message.answer("Телефон (или нажмите «Пропустить»):", reply_markup=ikb([[("Пропустить", "cl:skip")], CANCEL]))


async def _create(event, state, phone):
    data = await state.get_data()
    with session_scope() as s:
        c = client_svc.create_client(s, data["name"], phone)
        cid = c.id
        text = client_text(s, cid)
    await state.clear()
    await show(event, "Клиент создан.\n" + text, _card_kb(cid, 0, 0, False))


@router.callback_query(F.data == "cl:skip", ClientFlow.new_phone)
async def skip_phone(cb: CallbackQuery, state: FSMContext):
    await _create(cb, state, None)


@router.message(ClientFlow.new_phone, F.text)
async def got_phone(message: Message, state: FSMContext):
    await _create(message, state, message.text)


def _card_kb(cid, debt, credit, archived):
    rows = [[("Принять оплату", f"pay:c:{cid}"), ("Заказы", f"cl:o:{cid}")],
            [("История", f"cl:h:{cid}")]]
    if credit > 0:
        rows.append([("Вернуть переплату", f"cl:rf:{cid}")])
    rows.append([("Имя", f"cl:e:name:{cid}"), ("Телефон", f"cl:e:phone:{cid}"), ("Заметка", f"cl:e:note:{cid}")])
    rows.append([("Вернуть из архива", f"cl:un:{cid}")] if archived else [("В архив", f"cl:ar:{cid}")])
    return ikb(rows)


async def show_client(event, cid):
    with session_scope() as s:
        text = client_text(s, cid)
        c = client_svc.get_client(s, cid)
        debt, credit, arch = balance.client_debt(s, cid), balance.client_credit(s, cid), c.is_archived
    await show(event, text, _card_kb(cid, debt, credit, arch))


@router.callback_query(F.data.startswith("cl:c:"))
async def client_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_client(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("cl:h:"))
async def client_history(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    with session_scope() as s:
        ev = client_svc.history(s, cid)[:25]
    lines = [f"{fmt_dt(e['dt'])}  {e['text']}: {'+' if e['amount'] > 0 else ''}{fmt_money(e['amount'])}" for e in ev]
    await show(cb, "История (последние 25):\n\n" + ("\n".join(lines) or "Пока пусто."),
               ikb([[("Назад", f"cl:c:{cid}")]]))


@router.callback_query(F.data.startswith("cl:o:"))
async def client_orders(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    with session_scope() as s:
        rows = []
        for o in s.scalars(select(Order).where(Order.client_id == cid).order_by(Order.id.desc()).limit(20)):
            if o.status == "cancelled":
                label = f"№{o.id} {o.created_at:%d.%m} — отменён"
            else:
                d = balance.order_debt(s, o)
                label = f"№{o.id} {o.created_at:%d.%m} — {fmt_money(o.total_minor)}" + (f", долг {fmt_money(d)}" if d else ", оплачен")
            rows.append([(label, f"cl:od:{o.id}")])
    await show(cb, "Заказы (последние 20):" if rows else "Заказов нет.", ikb(rows + [[("Назад", f"cl:c:{cid}")]]))


@router.callback_query(F.data.startswith("cl:od:"))
async def order_card(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2])
    with session_scope() as s:
        o = s.get(Order, oid)
        text, cid, active = order_text(s, o), o.client_id, o.status == "active"
        debt = balance.order_debt(s, o)
    rows = []
    if active and debt > 0:
        rows.append([("Принять оплату", f"pay:o:{oid}"), ("Списать долг", f"cl:wo:{oid}")])
    if active:
        rows.append([("Отменить заказ", f"cl:cx:{oid}")])
    rows.append([("Назад", f"cl:o:{cid}" if cid else "x:cancel")])
    await show(cb, text, ikb(rows))


@router.callback_query(F.data.startswith("cl:cx:"))
async def cancel_ask(cb: CallbackQuery, state: FSMContext):
    oid = cb.data.split(":")[2]
    await show(cb, f"Отменить заказ №{oid}? Оплаченное станет переплатой клиента. Заказ останется в истории.",
               ikb([[("Да, отменить", f"cl:cxy:{oid}")], [("Нет", f"cl:od:{oid}")]]))


@router.callback_query(F.data.startswith("cl:cxy:"))
async def cancel_do(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2])
    with session_scope() as s:
        order_svc.cancel_order(s, oid, "Отмена владельцем")
        o = s.get(Order, oid)
        text, cid = order_text(s, o), o.client_id
    await show(cb, "Заказ отменён.\n\n" + text, ikb([[("К клиенту", f"cl:c:{cid}")]] if cid else []) if cid else None)


@router.callback_query(F.data.startswith("cl:wo:"))
async def writeoff_ask(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2])
    with session_scope() as s:
        debt = balance.order_debt(s, s.get(Order, oid))
    await state.set_state(ClientFlow.writeoff)
    await state.update_data(oid=oid, key=uuid.uuid4().hex)
    await show(cb, f"Долг по заказу №{oid}: {fmt_money(debt)}.\nСумма списания (не считается полученными деньгами):",
               ikb([CANCEL]))


@router.message(ClientFlow.writeoff, F.text)
async def writeoff_do(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = parse_money(message.text)
    with session_scope() as s:
        pay_svc.write_off(s, order_id=data["oid"], amount_minor=amount, reason="Списание владельцем", key=data["key"])
        text = order_text(s, s.get(Order, data["oid"]))
    await state.clear()
    await message.answer("Списано.\n\n" + text)


@router.callback_query(F.data.startswith("cl:rf:"))
async def refund_ask(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    with session_scope() as s:
        credit = balance.client_credit(s, cid)
    await state.set_state(ClientFlow.refund)
    await state.update_data(cid=cid, key=uuid.uuid4().hex)
    await show(cb, f"Переплата: {fmt_money(credit)}. Сколько вернуть клиенту?", ikb([CANCEL]))


@router.message(ClientFlow.refund, F.text)
async def refund_do(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = parse_money(message.text)
    with session_scope() as s:
        pay_svc.refund_credit(s, client_id=data["cid"], amount_minor=amount, key=data["key"])
    await state.clear()
    await message.answer(f"Возврат {fmt_money(amount)} записан.")
    await show_client(message, data["cid"])


@router.callback_query(F.data.startswith("cl:e:"))
async def edit_ask(cb: CallbackQuery, state: FSMContext):
    _, _, field, cid = cb.data.split(":")
    await state.set_state(ClientFlow.edit)
    await state.update_data(field=field, cid=int(cid))
    hint = " (для очистки отправьте «-»)" if field != "name" else ""
    await show(cb, f"Новое значение{hint}:", ikb([CANCEL]))


@router.message(ClientFlow.edit, F.text)
async def edit_do(message: Message, state: FSMContext):
    data = await state.get_data()
    with session_scope() as s:
        client_svc.update_client(s, data["cid"], data["field"], message.text)
    await state.clear()
    await show_client(message, data["cid"])


@router.callback_query(F.data.regexp(r"^cl:(ar|un):\d+$"))
async def archive_toggle(cb: CallbackQuery, state: FSMContext):
    _, act, cid = cb.data.split(":")
    with session_scope() as s:
        client_svc.set_archived(s, int(cid), act == "ar")
    await show_client(cb, int(cid))
