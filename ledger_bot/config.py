import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset
    database_path: str
    backup_dir: Path
    file_retention_days: int = 14


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    owners = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    if not owners:
        raise SystemExit("Не задан OWNER_IDS (см. .env.example)")
    return Config(
        bot_token=token,
        owner_ids=frozenset(owners),
        database_path=os.getenv("DATABASE_PATH", "data/ledger.db"),
        backup_dir=Path(os.getenv("BACKUP_DIR", "data/backups")),
        file_retention_days=max(1, int(os.getenv("FILE_RETENTION_DAYS", "14"))),
    )
