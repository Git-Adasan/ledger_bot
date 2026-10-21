import pytest

from ledger_bot.db.session import init_db, session_scope
from ledger_bot.services import catalog


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_db(path)
    with session_scope() as s:
        catalog.seed_defaults(s)
    return path


def svc(s, name):
    from sqlalchemy import select
    from ledger_bot.db.models import Service
    return s.scalar(select(Service).where(Service.name == name))
