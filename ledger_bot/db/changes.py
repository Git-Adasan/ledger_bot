"""Отслеживание изменений базы: нужно, чтобы вовремя отправлять копию владельцу."""
import threading
import time

_lock = threading.Lock()
_first = None  # когда появилось первое неотправленное изменение
_last = None   # когда было последнее


def mark_dirty() -> None:
    global _first, _last
    t = time.monotonic()
    with _lock:
        if _first is None:
            _first = t
        _last = t


def clear() -> None:
    global _first, _last
    with _lock:
        _first = _last = None


def should_send(quiet: float = 10.0, max_wait: float = 120.0, now=None) -> bool:
    """Копия нужна, когда изменения «затихли» на quiet секунд или ждём уже max_wait секунд."""
    now = time.monotonic() if now is None else now
    with _lock:
        if _first is None:
            return False
        return now - _last >= quiet or now - _first >= max_wait


def take_if_due(quiet: float = 10.0, max_wait: float = 120.0, now=None) -> bool:
    global _first, _last
    now = time.monotonic() if now is None else now
    with _lock:
        if _first is None or not (now - _last >= quiet or now - _first >= max_wait):
            return False
        _first = _last = None
        return True
