import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from ...db.session import init_db, session_scope
from ...errors import ServiceError
from ...services import backup, export
from ...timeutil import now
from ..common import show
from ..keyboards import CANCEL, ikb

router = Router()
CFG: dict = {}  # заполняется в main: {"db": путь, "backups": папка}
MAX_FILE = 50 * 1024 * 1024


class SettingsFlow(StatesGroup):
    wait_file = State()
    confirm = State()


async def start_settings(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Настройки и данные", reply_markup=ikb([
        [("Создать резервную копию", "bk:new")], [("Восстановить из копии", "bk:restore")]]))


async def start_export(message: Message, state: FSMContext):
    await state.clear()
    with session_scope() as s:
        data = export.export_zip(s)
    await message.answer_document(BufferedInputFile(data, f"export_{now():%Y%m%d_%H%M}.zip"),
                                  caption="Экспорт: CSV-файлы (открываются в Excel). Суммы *_minor — в тиынах.")


@router.callback_query(F.data == "bk:new")
async def make_backup(cb: CallbackQuery, state: FSMContext):
    path = backup.create_backup(CFG["db"], CFG["backups"])
    await cb.message.answer_document(FSInputFile(path), caption=f"Резервная копия {now():%d.%m.%Y %H:%M}. Храните её отдельно.")
    await cb.answer("Готово")


@router.callback_query(F.data == "bk:restore")
async def restore_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.wait_file)
    await show(cb, "Пришлите файл копии (.db) документом. Перед заменой я сохраню текущую базу отдельной копией.",
               ikb([CANCEL]))


@router.message(SettingsFlow.wait_file, F.document)
async def got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE:
        raise ServiceError("Файл слишком большой (лимит 50 МБ).")
    tmp = Path(tempfile.mkdtemp()) / "restore.db"
    await bot.download(doc, destination=tmp)
    backup.validate_db_file(tmp)  # понятная ошибка, пока ничего не тронуто
    await state.set_state(SettingsFlow.confirm)
    await state.update_data(path=str(tmp))
    await message.answer("Файл проверен. Заменить текущие данные данными из этой копии?",
                         reply_markup=ikb([[("Да, восстановить", "bk:yes")], CANCEL]))


@router.callback_query(F.data == "bk:yes", SettingsFlow.confirm)
async def restore_do(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    safety = backup.restore_backup(Path(d["path"]), CFG["db"], CFG["backups"])
    init_db(CFG["db"])  # переоткрыть соединения
    await state.clear()
    await show(cb, f"Данные восстановлены. Прежняя база сохранена: {safety.name}")
