"""Создание заказа и калькулятор — один и тот же конструктор позиций (mode: order | calc)."""
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...db.session import session_scope
from ...money import fmt_money, parse_qty
from ...services import catalog, clients as client_svc, orders as order_svc
from ...services.calc import CALC_TYPES, validate_qty
from ..common import show
from ..format import line_text, order_text, price_label
from ..keyboards import CANCEL, ikb

router = Router()


class OrderFlow(StatesGroup):
    client_search = State()
    new_client = State()
    qty = State()


def _draft_text(s, data) -> str:
    head = "Калькулятор" if data["mode"] == "calc" else "Новый заказ"
    if data["mode"] == "order":
        cid = data.get("client_id")
        head += " — " + (client_svc.get_client(s, cid).name if cid else "без клиента")
    if not data["items"]:
        return head + "\nПозиций пока нет."
    lines = order_svc.build_lines(s, data["items"])
    body = [line_text(i, l.name, l.qty_milli, l.unit, l.price_minor, l.total_minor,
                      fixed=CALC_TYPES[catalog.get_service(s, l.service_id).calc_type].key == "fixed")
            for i, l in enumerate(lines, 1)]
    return head + "\n" + "\n".join(body) + f"\n\nИтого: {fmt_money(sum(l.total_minor for l in lines))}"


async def _show_pick(event, state: FSMContext):
    data = await state.get_data()
    with session_scope() as s:
        text = _draft_text(s, data)
        cats = catalog.categories_with_services(s)
    rows = [[(c.name, f"oi:c:{c.id}")] for c in cats]
    if data["items"]:
        extra = [("Убрать последнюю", "od:undo")]
        if data["mode"] == "order":
            extra.insert(0, ("Оформить", "od:done"))
        else:
            extra.append(("Очистить", "od:reset"))
        rows.append(extra)
    rows.append(CANCEL)
    await show(event, text + "\n\nВыберите категорию услуги:" if cats else text + "\n\nВ каталоге нет доступных услуг.",
               ikb(rows))


async def start_order(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(mode="order", items=[], key=uuid.uuid4().hex, client_id=None)
    with session_scope() as s:
        recent = [(c.id, c.name) for c in client_svc.recent_clients(s, 6)]
    rows = [[("Без клиента (личная запись)", "oc:none")]]
    rows += [[(name, f"oc:{cid}")] for cid, name in recent]
    rows += [[("Найти клиента", "oc:find"), ("Новый клиент", "oc:new")], CANCEL]
    await message.answer("Для кого заказ?", reply_markup=ikb(rows))


async def start_calc(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(mode="calc", items=[], client_id=None)
    await _show_pick(message, state)


@router.callback_query(F.data.startswith("oc:"))
async def choose_client(cb: CallbackQuery, state: FSMContext):
    val = cb.data[3:]
    data = await state.get_data()
    if "items" not in data:
        await cb.answer("Начните заново: «Создать заказ»", show_alert=True)
        return
    if val == "find":
        await state.set_state(OrderFlow.client_search)
        await show(cb, "Введите часть имени или телефона:", ikb([CANCEL]))
    elif val == "new":
        await state.set_state(OrderFlow.new_client)
        await show(cb, "Введите имя нового клиента:", ikb([CANCEL]))
    else:
        await state.update_data(client_id=None if val == "none" else int(val))
        await _show_pick(cb, state)


@router.message(OrderFlow.client_search, F.text)
async def search_client(message: Message, state: FSMContext):
    with session_scope() as s:
        found = [(c.id, f"{c.name} {c.phone or ''}".strip())
                 for c in client_svc.search(s, message.text) if not c.is_archived]
    if not found:
        await message.answer("Никого не нашёл. Попробуйте другой запрос:", reply_markup=ikb([CANCEL]))
        return
    await message.answer("Выберите клиента:", reply_markup=ikb([[(n, f"oc:{i}")] for i, n in found] + [CANCEL]))


@router.message(OrderFlow.new_client, F.text)
async def new_client(message: Message, state: FSMContext):
    with session_scope() as s:
        c = client_svc.create_client(s, message.text)
        cid = c.id
    await state.update_data(client_id=cid)
    await state.set_state(None)
    await _show_pick(message, state)


@router.callback_query(F.data.startswith("oi:c:"))
async def pick_category(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    with session_scope() as s:
        rows = [[(f"{sv.name} — {price_label(sv)}", f"oi:s:{sv.id}")]
                for sv in catalog.list_services(s, category_id=cid, selectable=True)]
    await show(cb, "Выберите услугу:", ikb(rows + [[("Назад", "od:more")]]))


@router.callback_query(F.data.startswith("oi:s:"))
async def pick_service(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    with session_scope() as s:
        sv = catalog.get_service(s, sid)
        variants = [(v.id, f"{v.name} — {price_label(sv, v.price_minor)}") for v in catalog.active_variants(sv)]
    if variants:
        await show(cb, f"{sv.name}: выберите вариант", ikb(
            [[(t, f"oi:v:{sid}:{vid}")] for vid, t in variants] + [[("Назад", "od:more")]]))
    else:
        await _chosen(cb, state, sid, None)


@router.callback_query(F.data.startswith("oi:v:"))
async def pick_variant(cb: CallbackQuery, state: FSMContext):
    _, _, sid, vid = cb.data.split(":")
    await _chosen(cb, state, int(sid), int(vid))


async def _chosen(cb, state, sid, vid):
    data = await state.get_data()
    if "items" not in data:
        await cb.answer("Начните заново.", show_alert=True)
        return
    with session_scope() as s:
        sv = catalog.get_service(s, sid)
        ct, unit = sv.calc_type, sv.unit
    if ct == "fixed":
        items = data["items"] + [{"sid": sid, "vid": vid, "qty": 1000}]
        await state.update_data(items=items)
        await _show_pick(cb, state)
        return
    await state.update_data(pending={"sid": sid, "vid": vid})
    await state.set_state(OrderFlow.qty)
    hint = "можно дробное, например 1.5" if CALC_TYPES[ct].fraction else "целое число"
    await show(cb, f"Сколько ({unit})? {hint}", ikb([CANCEL]))


@router.message(OrderFlow.qty, F.text)
async def got_qty(message: Message, state: FSMContext):
    data = await state.get_data()
    qty = parse_qty(message.text)
    p = data["pending"]
    with session_scope() as s:
        validate_qty(catalog.get_service(s, p["sid"]).calc_type, qty)
    await state.update_data(items=data["items"] + [{"sid": p["sid"], "vid": p["vid"], "qty": qty}],
                            pending=None)
    await state.set_state(None)
    await _show_pick(message, state)


@router.callback_query(F.data.in_({"od:more", "od:undo", "od:reset"}))
async def draft_actions(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "items" not in data:
        await cb.answer("Начните заново.", show_alert=True)
        return
    if cb.data == "od:undo":
        await state.update_data(items=data["items"][:-1])
    elif cb.data == "od:reset":
        await state.update_data(items=[])
    await _show_pick(cb, state)


@router.callback_query(F.data == "od:done")
async def confirm_screen(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") != "order" or not data.get("items"):
        await cb.answer("Нет позиций.", show_alert=True)
        return
    with session_scope() as s:
        text = _draft_text(s, data)
    await show(cb, text + "\n\nОформить заказ?", ikb([
        [("Подтвердить", "od:ok")], [("Добавить ещё", "od:more"), ("Отмена", "x:cancel")]]))


@router.callback_query(F.data == "od:ok")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") != "order" or not data.get("items"):
        await cb.answer("Этот заказ уже оформлен или отменён.", show_alert=True)
        return
    with session_scope() as s:
        order, created = order_svc.create_order(
            s, items=data["items"], client_id=data.get("client_id"), key=data["key"])
        text = order_text(s, order)
        oid, has_client = order.id, order.client_id is not None
    await state.clear()
    rows = [[("Принять оплату", f"pay:o:{oid}")]] if has_client else []
    await show(cb, ("Заказ создан.\n\n" if created else "Заказ уже был создан.\n\n") + text,
               ikb(rows) if rows else None)
