from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...db.session import session_scope
from ...money import fmt_money
from ...services import clients as client_svc
from ..common import show
from ..keyboards import MENU_ITEMS, main_menu
from . import catalog, clients, finance, orders, payments, settings

router = Router()

ACTIONS = {
    "Создать заказ": orders.start_order,
    "Клиенты": clients.start_clients,
    "Должники": clients.start_debtors,
    "Приём оплаты": payments.start_payment,
    "Каталог услуг": catalog.start_catalog,
    "Калькулятор": orders.start_calc,
    "Доходы и расходы": finance.start_fin,
    "Финансовая статистика": finance.start_stats,
    "Экспорт": settings.start_export,
    "Настройки": settings.start_settings,
}
assert set(ACTIONS) == set(MENU_ITEMS)


@router.message(CommandStart())
@router.message(Command("menu"))
@router.message(Command("cancel"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    with session_scope() as s:
        debtors = client_svc.debtors_with_since(s)
    total = sum(d for _, d, _ in debtors)
    debts = (f"Вам должны: {fmt_money(total)} (человек: {len(debtors)})" if debtors else "Долгов сейчас нет.")
    await message.answer("Здравствуйте! Это ваш личный бот учёта заказов, денег и долгов.\n"
                         f"{debts}\n\nВыберите действие в меню.", reply_markup=main_menu())


@router.callback_query(F.data == "x:cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Отменено.")


# Кнопки меню работают в любом состоянии и прерывают незаконченный ввод.
@router.message(F.text.in_(set(MENU_ITEMS)))
async def menu_press(message: Message, state: FSMContext):
    await state.clear()
    await ACTIONS[message.text](message, state)
