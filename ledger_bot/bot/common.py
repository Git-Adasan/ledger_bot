from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery


async def show(event, text: str, markup=None):
    """Для кнопки — правит сообщение, для текста — отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)
