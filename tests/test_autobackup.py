import time

from sqlalchemy import text

from ledger_bot.autobackup import send_backup_once
from ledger_bot.db import changes
from ledger_bot.db.session import session_scope
from ledger_bot.services import clients
from .test_bot_flow import Harness, run


def test_only_successful_writes_mark_dirty(db):
    changes.clear()
    with session_scope() as s:
        s.execute(text("SELECT 1"))
    assert not changes.should_send(now=time.monotonic() + 1000)  # чтение копию не вызывает

    with session_scope() as s:
        clients.create_client(s, "A")
    t = time.monotonic()
    assert not changes.should_send(quiet=10, now=t)            # только что изменили — ждём тишины
    assert changes.should_send(quiet=10, now=t + 11)
    assert changes.take_if_due(now=t + 11)
    assert not changes.take_if_due(now=t + 12)                 # одна копия на пачку изменений

    changes.clear()
    try:
        with session_scope() as s:
            clients.create_client(s, "B")
            raise RuntimeError("сбой")
    except RuntimeError:
        pass
    assert not changes.should_send(now=time.monotonic() + 1000)  # откат — копия не нужна


def test_max_wait_when_changes_never_stop(db):
    changes.clear()
    changes.mark_dirty()
    t = time.monotonic()
    assert changes.should_send(quiet=10, max_wait=120, now=t + 121)


def test_send_backup_to_every_owner_silently(db, tmp_path):
    with session_scope() as s:
        clients.create_client(s, "Айгерим")

    async def go():
        h = Harness()
        await send_backup_once(h.bot, [1, 2], db, tmp_path / "bk")
        return h.session.calls
    docs = [c for c in run(go()) if type(c).__name__ == "SendDocument"]
    assert len(docs) == 2 and all(c.disable_notification for c in docs)
    assert "Клиентов: 1" in docs[0].caption
