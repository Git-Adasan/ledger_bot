from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import changes
from .models import Base

_engine = None
_factory = None


@event.listens_for(Session, "after_flush")
def _remember_write(session, flush_context):
    session.info["wrote"] = True  # в этой транзакции что-то записывалось


def init_db(path: str):
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 15})

    @event.listens_for(_engine, "connect")
    def _pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    _factory = sessionmaker(_engine, expire_on_commit=False)


def dispose_engine():
    if _engine is not None:
        _engine.dispose()


@contextmanager
def session_scope():
    """Одна операция = одна транзакция: при любой ошибке откат целиком."""
    s = _factory()
    try:
        yield s
        s.commit()
        if s.info.get("wrote"):
            changes.mark_dirty()  # только после успешной фиксации
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
