from aiogram import BaseMiddleware


class OwnerOnly(BaseMiddleware):
    """Бот личный: обновления от всех, кроме владельца, молча игнорируются."""

    def __init__(self, owner_ids):
        self.owner_ids = set(owner_ids)

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in self.owner_ids:
            return None
        return await handler(event, data)
