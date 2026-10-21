import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from aiohttp import web
from sqlalchemy import text

from pathlib import Path

from .autobackup import autosend_loop
from .bot.access import OwnerOnly
from .bot.handlers import catalog, clients, finance, menu, orders, payments, settings
from .config import load_config
from .db import changes
from .db.session import init_db, session_scope
from .errors import ServiceError
from .services import backup, catalog as catalog_svc

log = logging.getLogger("ledger_bot")


def build_dispatcher(owner_ids) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnly(owner_ids))
    for r in (menu.router, orders.router, clients.router, payments.router,
              catalog.router, finance.router, settings.router):
        dp.include_router(r)  # menu первым: кнопки меню прерывают любой ввод
    dp.errors.register(on_error)
    return dp


async def on_error(event: ErrorEvent):
    exc, upd = event.exception, event.update
    cb = upd.callback_query
    chat = (upd.message or (cb.message if cb else None))
    user_error = isinstance(exc, ServiceError)
    if not user_error:
        log.exception("Необработанная ошибка", exc_info=exc)
    text = str(exc) if user_error else "Произошла ошибка. Операция не выполнена, данные не изменены."
    if cb:
        await cb.answer(text[:190], show_alert=True)
    elif chat:
        await chat.answer(text)
    return True


def _db_ping():
    with session_scope() as s:
        s.execute(text("SELECT 1"))


def build_health_app() -> web.Application:
    """Адрес для UptimeRobot: 200 «ok», если бот жив и база открывается; иначе 503. Данных не отдаёт."""
    async def health(request):
        try:
            await asyncio.to_thread(_db_ping)
        except Exception:
            return web.Response(status=503, text="db error")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)  # HEAD aiohttp добавляет сам
    return app


async def backup_loop(db, directory):
    while True:
        try:
            await asyncio.to_thread(backup.create_backup, db, directory)
        except Exception:
            log.exception("Автокопия не удалась")
        await asyncio.sleep(24 * 3600)


async def main():
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    fresh = not Path(cfg.database_path).exists()  # на бесплатном хостинге файл мог быть стёрт
    init_db(cfg.database_path)
    with session_scope() as s:
        catalog_svc.seed_defaults(s)
    changes.clear()  # стартовые данные — не повод отправлять копию
    settings.CFG.update(db=cfg.database_path, backups=cfg.backup_dir)
    bot = Bot(cfg.bot_token)
    dp = build_dispatcher(cfg.owner_ids)
    task = asyncio.create_task(backup_loop(cfg.database_path, cfg.backup_dir))
    send_task = asyncio.create_task(autosend_loop(bot, cfg.owner_ids, cfg.database_path, cfg.backup_dir))
    if fresh:
        for oid in cfg.owner_ids:
            try:
                await bot.send_message(
                    oid, "База данных пустая: первый запуск или хостинг стёр файлы.\n"
                         "Если первый запуск — ничего не делайте. Если данные были раньше, "
                         "до начала работы пришлите последний файл копии: Настройки → Восстановить из копии.")
            except Exception:
                log.warning("Не удалось написать владельцу %s (он должен сначала нажать /start)", oid)
    runner = None
    port = os.getenv("PORT") or os.getenv("HEALTH_PORT")  # без порта адрес не поднимается
    if port:
        runner = web.AppRunner(build_health_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(port)).start()
        log.info("Адрес для мониторинга: порт %s, путь /health", port)
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        send_task.cancel()
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
