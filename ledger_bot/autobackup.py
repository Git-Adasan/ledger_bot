"""Бесплатные хостинги стирают файлы при перезапуске, поэтому копия базы уходит владельцу в Telegram."""
import asyncio
import logging

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import func, select

from .db import changes
from .db.models import Client, Order, Payment
from .db.session import session_scope
from .services import backup
from .timeutil import now

log = logging.getLogger("ledger_bot")


def _caption() -> str:
    with session_scope() as s:
        c = s.scalar(select(func.count(Client.id)))
        o = s.scalar(select(func.count(Order.id)))
        p = s.scalar(select(func.count(Payment.id)))
    return (f"Копия базы {now():%d.%m.%Y %H:%M}\nКлиентов: {c}, заказов: {o}, платежей: {p}\n"
            "Для восстановления берите файл, где данных больше всего: Настройки → Восстановить из копии.")


async def send_backup_once(bot: Bot, owner_ids, db_path, backup_dir) -> None:
    path = await asyncio.to_thread(backup.create_backup, db_path, backup_dir)
    caption = await asyncio.to_thread(_caption)
    for oid in owner_ids:
        await bot.send_document(oid, FSInputFile(path), caption=caption, disable_notification=True)


async def autosend_loop(bot: Bot, owner_ids, db_path, backup_dir) -> None:
    while True:
        await asyncio.sleep(5)
        if changes.take_if_due():
            try:
                await send_backup_once(bot, owner_ids, db_path, backup_dir)
            except Exception:
                log.exception("Не удалось отправить копию, попробую позже")
                changes.mark_dirty()
                await asyncio.sleep(30)
