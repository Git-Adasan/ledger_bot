import sqlite3
from pathlib import Path

from ..db.models import Base
from ..db.session import dispose_engine
from ..errors import ServiceError
from ..timeutil import now


def create_backup(db_path: str, backup_dir: Path, keep: int = 30) -> Path:
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"ledger_{now():%Y%m%d_%H%M%S}.db"
    i = 1
    while dest.exists():
        dest = backup_dir / f"ledger_{now():%Y%m%d_%H%M%S}_{i}.db"
        i += 1
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)  # согласованная копия даже при работающем боте
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob("ledger_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


# таблицы, добавленные позже: в старых копиях их нет, при восстановлении они создаются заново
OPTIONAL_TABLES = {"attachments"}


def validate_db_file(path: Path) -> None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ServiceError("Файл повреждён (integrity_check).")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError:
        raise ServiceError("Это не файл базы SQLite.")
    missing = set(Base.metadata.tables) - OPTIONAL_TABLES - tables
    if missing:
        raise ServiceError("Файл не похож на копию этого бота (нет таблиц: "
                           + ", ".join(sorted(missing)) + ").")


def restore_backup(src_path: Path, db_path: str, backup_dir: Path) -> Path:
    """Проверяет файл, делает страховочную копию текущей базы и заменяет её содержимое."""
    validate_db_file(src_path)
    safety = create_backup(db_path, backup_dir)
    dispose_engine()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return safety
