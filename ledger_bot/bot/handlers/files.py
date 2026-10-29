"""Файлы заказов и чеки оплат.

Бот не скачивает файлы: он запоминает ссылку Telegram (file_id) и имя. Диск сервера не растёт.
Через FILE_RETENTION_DAYS дней ссылка стирается, запись об оплате и файле остаётся.
"""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from ...db.models import Client, Order, Payment
from ...db.session import session_scope
from ...money import fmt_money
from ...services import attachments
from ..common import show
from ..keyboards import CANCEL, ikb
from . import orders

router = Router()
ANY_FILE = F.document | F.photo


class FileFlow(StatesGroup):
    choose = State()  # файл прислали «просто так» — спрашиваем, куда его сохранить
    draft = State()   # файлы к заказу, который ещё оформляется
    wait = State()    # файлы к уже существующему заказу или чек к платежу


def extract(message: Message) -> dict | None:
    """Достаёт из сообщения всё нужное для повторной отправки (без скачивания)."""
    if message.document:
        d = message.document
        return dict(media="document", file_id=d.file_id, unique_id=d.file_unique_id,
                    file_name=d.file_name, size=d.file_size)
    if message.photo:
        p = message.photo[-1]  # самый большой размер
        return dict(media="photo", file_id=p.file_id, unique_id=p.file_unique_id,
                    file_name=None, size=p.file_size)
    return None


def _label(f: dict) -> str:
    return f.get("file_name") or ("фото" if f["media"] == "photo" else "файл")


def _keep_note() -> str:
    return f"Хранится {attachments.RETENTION_DAYS} дн., потом ссылка удаляется (запись о файле остаётся)."


# ---------- файл прислали без контекста ----------
@router.message(StateFilter(None), ANY_FILE)
async def stray_file(message: Message, state: FSMContext):
    f = extract(message)
    await state.set_state(FileFlow.choose)
    await state.update_data(files=[f])
    await message.answer(f"Куда сохранить «{_label(f)}»?", reply_markup=ikb([
        [("Новый заказ с этим файлом", "fl:new")],
        [("К существующему заказу", "fl:lo")],
        [("Это чек об оплате", "fl:lp")], CANCEL]))


@router.message(FileFlow.choose, ANY_FILE)
async def more_stray(message: Message, state: FSMContext):
    data = await state.get_data()
    files = data["files"] + [extract(message)]
    await state.update_data(files=files)
    await message.answer(f"Добавлено. Файлов: {len(files)}. Выберите, куда сохранить, в сообщении выше.")


@router.callback_query(F.data == "fl:new", FileFlow.choose)
async def stray_new_order(cb: CallbackQuery, state: FSMContext):
    files = (await state.get_data())["files"]
    await orders.start_order(cb.message, state)
    await state.update_data(files=files)
    await cb.answer()


@router.callback_query(F.data == "fl:lo", FileFlow.choose)
async def stray_pick_order(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = []
        for o in s.scalars(select(Order).where(Order.status == "active").order_by(Order.id.desc()).limit(8)):
            who = s.get(Client, o.client_id).name if o.client_id else "без клиента"
            rows.append([(f"№{o.id} {o.created_at:%d.%m} {who} — {fmt_money(o.total_minor)}", f"fl:to:o:{o.id}")])
    await show(cb, "Выберите заказ (последние 8):" if rows else "Заказов пока нет.", ikb(rows + [CANCEL]))


@router.callback_query(F.data == "fl:lp", FileFlow.choose)
async def stray_pick_payment(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = []
        for p in s.scalars(select(Payment).where(Payment.kind == "payment", Payment.client_id.is_not(None))
                           .order_by(Payment.id.desc()).limit(8)):
            who = s.get(Client, p.client_id).name
            rows.append([(f"{p.created_at:%d.%m %H:%M} {who} — {fmt_money(p.amount_minor)}", f"fl:to:p:{p.id}")])
    await show(cb, "К какой оплате относится чек (последние 8):" if rows else "Оплат пока нет.",
               ikb(rows + [CANCEL]))


@router.callback_query(F.data.regexp(r"^fl:to:[op]:\d+$"), FileFlow.choose)
async def stray_save(cb: CallbackQuery, state: FSMContext):
    _, _, kind, tid = cb.data.split(":")
    files = (await state.get_data()).get("files") or []
    n = _save(files, kind, int(tid))
    await state.clear()
    await show(cb, _saved_text(n, kind, int(tid)))


# ---------- файлы к заказу, который оформляется ----------
@router.callback_query(F.data == "fl:draft")
async def draft_ask(cb: CallbackQuery, state: FSMContext):
    if (await state.get_data()).get("mode") != "order":
        await cb.answer("Начните заново: «Создать заказ»", show_alert=True)
        return
    await state.set_state(FileFlow.draft)
    await show(cb, "Пришлите файл клиента (документом или фото). Можно несколько.\n" + _keep_note(),
               ikb([[("Готово", "fl:done")], CANCEL]))


@router.message(FileFlow.draft, ANY_FILE)
async def draft_file(message: Message, state: FSMContext):
    data = await state.get_data()
    f = extract(message)
    files = [x for x in data.get("files") or [] if x["unique_id"] != f["unique_id"]] + [f]
    await state.update_data(files=files)
    await message.answer(f"Добавлено: {_label(f)}. Файлов: {len(files)}.",
                         reply_markup=ikb([[("Готово", "fl:done")], CANCEL]))


@router.callback_query(F.data == "fl:done", FileFlow.draft)
async def draft_done(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await orders.show_confirm(cb, state)


# ---------- файл к существующему заказу / чек к платежу ----------
@router.callback_query(F.data.regexp(r"^fl:(ao|rc):\d+$"))
async def attach_ask(cb: CallbackQuery, state: FSMContext):
    _, kind, tid = cb.data.split(":")
    await state.clear()
    await state.set_state(FileFlow.wait)
    await state.update_data(kind="o" if kind == "ao" else "p", tid=int(tid), n=0)
    what = f"файл для заказа №{tid}" if kind == "ao" else "чек об оплате (скриншот, фото или PDF)"
    await show(cb, f"Пришлите {what}.\n" + _keep_note(), ikb([[("Готово", "fl:fin")], CANCEL]))


@router.message(FileFlow.wait, ANY_FILE)
async def wait_file(message: Message, state: FSMContext):
    data = await state.get_data()
    f = extract(message)
    added = _save([f], data["kind"], data["tid"])
    n = data.get("n", 0) + added
    await state.update_data(n=n)
    await message.answer(("Сохранено: " if added else "Такой файл уже есть: ") + _label(f)
                         + ". Можно прислать ещё или нажать «Готово».",
                         reply_markup=ikb([[("Готово", "fl:fin")], CANCEL]))


@router.callback_query(F.data == "fl:fin", FileFlow.wait)
async def wait_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    rows = [[("Открыть заказ", f"cl:od:{data['tid']}")]] if data["kind"] == "o" else []
    await show(cb, f"Готово. Сохранено файлов: {data.get('n', 0)}.", ikb(rows) if rows else None)


def _save(files: list[dict], kind: str, tid: int) -> int:
    n = 0
    with session_scope() as s:
        for f in files:
            n += attachments.add(s, order_id=tid if kind == "o" else None,
                                 payment_id=tid if kind == "p" else None, **f)[1]
    return n


def _saved_text(n: int, kind: str, tid: int) -> str:
    where = f"к заказу №{tid}" if kind == "o" else "как чек к оплате"
    return f"Сохранено файлов: {n} ({where}).\n" + _keep_note()


# ---------- показать всё, что есть по заказу ----------
@router.callback_query(F.data.startswith("fl:show:"))
async def show_proof(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2])
    items, gone = [], 0
    with session_scope() as s:
        for a in attachments.for_order(s, oid):
            items.append((a.media, a.file_id, f"Файл клиента, заказ №{oid}: {a.file_name or ''}".strip(": ")))
        for p, amount, receipts in attachments.order_payments(s, oid):
            for a in receipts:
                items.append((a.media, a.file_id,
                              f"Чек: оплата {p.created_at:%d.%m.%Y %H:%M}, {fmt_money(p.amount_minor)}"))
    await cb.answer()
    if not items:
        await cb.message.answer("К этому заказу файлов и чеков не прикреплено.")
        return
    for media, file_id, caption in items:
        if not file_id:
            gone += 1
            continue
        try:
            if media == "photo":
                await cb.message.answer_photo(file_id, caption=caption)
            else:
                await cb.message.answer_document(file_id, caption=caption)
        except TelegramBadRequest:
            gone += 1
    if gone:
        await cb.message.answer(f"Не удалось показать файлов: {gone} (срок хранения вышел или Telegram "
                                "больше не отдаёт файл). Запись о них в карточке заказа осталась.")
