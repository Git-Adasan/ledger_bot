import uuid
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...db.models import Transaction
from ...db.session import session_scope
from ...errors import ServiceError
from ...money import fmt_money, parse_money
from ...services import finance as fin, stats
from ...timeutil import today
from ..common import show
from ..keyboards import CANCEL, ikb

router = Router()
KIND = {"income": "Доход", "expense": "Расход"}


class FinFlow(StatesGroup):
    amount = State()
    date_text = State()
    category_text = State()
    desc = State()
    note = State()


async def start_fin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Доходы и расходы (вне заказов)", reply_markup=ikb([
        [("+ Доход", "fi:income"), ("+ Расход", "fi:expense")], [("Последние операции", "fi:list")]]))


@router.callback_query(F.data.in_({"fi:income", "fi:expense"}))
async def begin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(FinFlow.amount)
    await state.update_data(kind=cb.data[3:], key=uuid.uuid4().hex)
    await show(cb, f"{KIND[cb.data[3:]]}. Сумма в ₸:", ikb([CANCEL]))


@router.message(FinFlow.amount, F.text)
async def got_amount(message: Message, state: FSMContext):
    await state.update_data(amount=parse_money(message.text))
    await message.answer("Дата операции:", reply_markup=ikb([
        [("Сегодня", "fd:today"), ("Вчера", "fd:yest")], [("Другая дата", "fd:other")], CANCEL]))


async def _ask_category(event, state):
    d = await state.get_data()
    with session_scope() as s:
        cats = fin.known_categories(s, d["kind"])
    await state.update_data(cats=cats)
    rows = [[(c, f"fc:{i}")] for i, c in enumerate(cats)] + [[("Другая категория", "fc:new")], CANCEL]
    await show(event, "Категория:", ikb(rows))


@router.callback_query(F.data.in_({"fd:today", "fd:yest"}))
async def date_quick(cb: CallbackQuery, state: FSMContext):
    d = today() - timedelta(days=1 if cb.data == "fd:yest" else 0)
    await state.update_data(day=d.isoformat())
    await _ask_category(cb, state)


@router.callback_query(F.data == "fd:other")
async def date_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FinFlow.date_text)
    await show(cb, "Введите дату в формате ДД.ММ.ГГГГ:", ikb([CANCEL]))


@router.message(FinFlow.date_text, F.text)
async def date_text(message: Message, state: FSMContext):
    try:
        d = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        raise ServiceError("Не понял дату. Пример: 21.10.2026")
    if d > today():
        raise ServiceError("Дата не может быть в будущем.")
    await state.update_data(day=d.isoformat())
    await _ask_category(message, state)


@router.callback_query(F.data.startswith("fc:"))
async def category(cb: CallbackQuery, state: FSMContext):
    val = cb.data[3:]
    if val == "new":
        await state.set_state(FinFlow.category_text)
        await show(cb, "Название категории:", ikb([CANCEL]))
        return
    d = await state.get_data()
    await state.update_data(category=d["cats"][int(val)])
    await _ask_desc(cb, state)


@router.message(FinFlow.category_text, F.text)
async def category_text(message: Message, state: FSMContext):
    if not message.text.strip() or len(message.text.strip()) > 80:
        raise ServiceError("Категория: от 1 до 80 символов.")
    await state.update_data(category=message.text.strip())
    await _ask_desc(message, state)


async def _ask_desc(event, state):
    await state.set_state(FinFlow.desc)
    await show(event, "Описание:", ikb([[("Пропустить", "fs:desc")], CANCEL]))


@router.callback_query(F.data == "fs:desc", FinFlow.desc)
async def skip_desc(cb: CallbackQuery, state: FSMContext):
    await state.update_data(desc=None)
    await _ask_note(cb, state)


@router.message(FinFlow.desc, F.text)
async def got_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await _ask_note(message, state)


async def _ask_note(event, state):
    await state.set_state(FinFlow.note)
    await show(event, "Примечание:", ikb([[("Пропустить", "fs:note")], CANCEL]))


async def _save(event, state, note):
    d = await state.get_data()
    with session_scope() as s:
        t, created = fin.add_transaction(
            s, kind=d["kind"], amount_minor=d["amount"], day=datetime.fromisoformat(d["day"]).date(),
            category=d["category"], description=d.get("desc"), note=note, key=d["key"])
        text = f"{KIND[t.kind]} {fmt_money(t.amount_minor)} · {t.date:%d.%m.%Y} · {t.category}"
    await state.clear()
    await show(event, ("Записано: " if created else "Уже записано: ") + text)


@router.callback_query(F.data == "fs:note", FinFlow.note)
async def skip_note(cb: CallbackQuery, state: FSMContext):
    await _save(cb, state, None)


@router.message(FinFlow.note, F.text)
async def got_note(message: Message, state: FSMContext):
    await _save(message, state, message.text)


@router.callback_query(F.data == "fi:list")
async def recent(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = [[(("✗ " if t.voided_at else "") + f"{t.date:%d.%m} {'+' if t.kind == 'income' else '−'}"
                  f"{fmt_money(t.amount_minor)} {t.category}", f"fo:{t.id}")] for t in fin.recent(s, 10)]
    await show(cb, "Последние операции:" if rows else "Операций пока нет.", ikb(rows) if rows else None)


@router.callback_query(F.data.regexp(r"^fo:\d+$"))
async def op_card(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data[3:])
    with session_scope() as s:
        t = s.get(Transaction, tid)
        text = (f"{KIND[t.kind]} {fmt_money(t.amount_minor)}\nДата: {t.date:%d.%m.%Y}\nКатегория: {t.category}\n"
                f"Описание: {t.description or '—'}\nПримечание: {t.note or '—'}"
                + ("\nАННУЛИРОВАНА" if t.voided_at else ""))
        voided = t.voided_at is not None
    await show(cb, text, None if voided else ikb([[("Аннулировать", f"fov:{tid}")]]))


@router.callback_query(F.data.startswith("fov:"))
async def void(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        fin.void_transaction(s, int(cb.data[4:]))
    await show(cb, "Операция аннулирована (останется в истории, в статистику не входит).")


# ---------- статистика ----------
async def start_stats(message: Message, state: FSMContext):
    await state.clear()
    keys = list(stats.PERIODS)
    await message.answer("За какой период?", reply_markup=ikb(
        [[(stats.PERIODS[k], f"st:{k}") for k in keys[:3]], [(stats.PERIODS[k], f"st:{k}") for k in keys[3:]]]))


@router.callback_query(F.data.startswith("st:"))
async def stats_show(cb: CallbackQuery, state: FSMContext):
    key = cb.data[3:]
    with session_scope() as s:
        st = stats.compute(s, *stats.period_range(key, today()))
        text = stats.format_stats(st, stats.PERIODS[key])
    await show(cb, text)
