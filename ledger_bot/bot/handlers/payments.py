import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...db.models import Order
from ...db.session import session_scope
from ...money import fmt_money, parse_money
from ...services import balance, clients as client_svc, payments as pay_svc
from ..common import show
from ..keyboards import CANCEL, ikb

router = Router()


class PayFlow(StatesGroup):
    search = State()
    amount = State()


async def start_payment(message: Message, state: FSMContext):
    await state.clear()
    with session_scope() as s:
        rows = [(c.id, c.name, d) for c, d in client_svc.debtors(s)[:15]]
    kb = [[(f"{n} — {fmt_money(d)}", f"pc:{i}")] for i, n, d in rows]
    kb.append([("Найти другого клиента", "pc:find")])
    kb.append(CANCEL)
    await message.answer("Кто платит?" if rows else "Должников нет. Можно принять предоплату — найдите клиента.",
                         reply_markup=ikb(kb))


@router.callback_query(F.data == "pc:find")
async def find_prompt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PayFlow.search)
    await show(cb, "Введите часть имени или телефона:", ikb([CANCEL]))


@router.message(PayFlow.search, F.text)
async def find(message: Message, state: FSMContext):
    with session_scope() as s:
        found = [(c.id, c.name) for c in client_svc.search(s, message.text) if not c.is_archived]
    if not found:
        await message.answer("Никого не нашёл. Другой запрос:", reply_markup=ikb([CANCEL]))
        return
    await message.answer("Выберите клиента:", reply_markup=ikb([[(n, f"pc:{i}")] for i, n in found] + [CANCEL]))


async def _begin(event, state, cid, preselect=None):
    await state.clear()
    await state.update_data(cid=cid, selected=[preselect] if preselect else [], key=uuid.uuid4().hex)
    await _show_orders(event, state)


async def _show_orders(event, state):
    data = await state.get_data()
    with session_scope() as s:
        name = client_svc.get_client(s, data["cid"]).name
        open_ = [(o.id, o.created_at, d) for o, d in balance.open_orders(s, data["cid"])]
        credit = balance.client_credit(s, data["cid"])
    rows = []
    for oid, dt, d in open_:
        mark = "✅ " if oid in data["selected"] else ""
        rows.append([(f"{mark}№{oid} {dt:%d.%m} — долг {fmt_money(d)}", f"po:{oid}")])
    rows.append([("Далее", "po:next")])
    rows.append(CANCEL)
    if open_:
        text = (f"{name}\nОтметьте заказы, которые оплачивает клиент. "
                "Если ничего не отмечать, деньги пойдут на самые старые долги.")
    else:
        text = f"{name}\nОткрытых долгов нет — платёж будет учтён как предоплата."
    if credit > 0:
        text += f"\nПереплата клиента: {fmt_money(credit)}"
    await show(event, text, ikb(rows))


@router.callback_query(F.data.startswith("pc:") & (F.data != "pc:find"))
async def pick_client(cb: CallbackQuery, state: FSMContext):
    await _begin(cb, state, int(cb.data[3:]))


@router.callback_query(F.data.startswith("pay:"))
async def from_card(cb: CallbackQuery, state: FSMContext):
    _, kind, val = cb.data.split(":")
    if kind == "c":
        await _begin(cb, state, int(val))
    else:
        with session_scope() as s:
            o = s.get(Order, int(val))
            cid = o.client_id
        await _begin(cb, state, cid, int(val))


@router.callback_query(F.data.startswith("po:"))
async def toggle_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "cid" not in data:
        await cb.answer("Начните заново: «Приём оплаты»", show_alert=True)
        return
    val = cb.data[3:]
    if val == "next":
        with session_scope() as s:
            debt = sum(balance.order_debt(s, s.get(Order, oid)) for oid in data["selected"]) if data["selected"] \
                else balance.client_debt(s, data["cid"])
        await state.set_state(PayFlow.amount)
        kb = [[(f"Вся сумма: {fmt_money(debt)}", "pa:full")]] if debt > 0 else []
        await show(cb, f"Долг {'по выбранным' if data['selected'] else 'клиента'}: {fmt_money(debt)}.\nВведите сумму оплаты:",
                   ikb(kb + [CANCEL]))
        return
    oid = int(val)
    sel = [x for x in data["selected"] if x != oid] if oid in data["selected"] else data["selected"] + [oid]
    await state.update_data(selected=sel)
    await _show_orders(cb, state)


async def _confirm(event, state, amount):
    data = await state.get_data()
    await state.update_data(amount=amount)
    with session_scope() as s:
        name = client_svc.get_client(s, data["cid"]).name
    target = ("заказы: " + ", ".join(f"№{x}" for x in data["selected"])) if data["selected"] else "самые старые долги"
    await show(event, f"Принять {fmt_money(amount)} от клиента {name}?\nНаправить на: {target}.",
               ikb([[("Подтвердить", "pk:ok")], CANCEL]))


@router.callback_query(F.data == "pa:full")
async def pay_full(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    with session_scope() as s:
        debt = sum(balance.order_debt(s, s.get(Order, oid)) for oid in data["selected"]) if data["selected"] \
            else balance.client_debt(s, data["cid"])
    if debt <= 0:
        await cb.answer("Долга нет.", show_alert=True)
        return
    await _confirm(cb, state, debt)


@router.message(PayFlow.amount, F.text)
async def pay_amount(message: Message, state: FSMContext):
    await _confirm(message, state, parse_money(message.text))


@router.callback_query(F.data == "pk:ok")
async def pay_ok(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "amount" not in data:
        await cb.answer("Эта оплата уже проведена или отменена.", show_alert=True)
        return
    with session_scope() as s:
        res = pay_svc.accept_payment(s, client_id=data["cid"], amount_minor=data["amount"],
                                     key=data["key"], order_ids=data["selected"] or None)
        lines = [f"Заказ №{oid}: {fmt_money(a)}" for oid, a in res.allocations]
        debt = balance.client_debt(s, data["cid"])
        credit = res.credit_after
        dup = res.duplicate
        pid = res.payment.id
    await state.clear()
    text = ("Эта оплата уже была записана.\n" if dup else "Оплата принята.\n") + "\n".join(lines)
    text += f"\n\nОстаток долга клиента: {fmt_money(debt)}"
    if credit > 0:
        text += f"\nПереплата: {fmt_money(credit)}"
    await show(cb, text + "\n\nМожно сохранить чек: нажмите кнопку и пришлите скриншот или PDF.",
               ikb([[("Прикрепить чек", f"fl:rc:{pid}")]]))
