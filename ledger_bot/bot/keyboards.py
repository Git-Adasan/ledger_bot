from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

MENU_ROWS = [
    ["Создать заказ", "Клиенты"],
    ["Должники", "Приём оплаты"],
    ["Каталог услуг", "Калькулятор"],
    ["Доходы и расходы", "Финансовая статистика"],
    ["Экспорт", "Настройки"],
]
MENU_ITEMS = [t for row in MENU_ROWS for t in row]
CANCEL = [("Отмена", "x:cancel")]


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in MENU_ROWS],
        resize_keyboard=True)


def ikb(rows) -> InlineKeyboardMarkup:
    """rows: список рядов, ряд — список (текст, callback_data)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])
