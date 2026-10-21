"""Деньги хранятся целым числом тиын (1 ₸ = 100), количество — тысячными долями."""
from decimal import Decimal, InvalidOperation

from .errors import ServiceError

MAX_MINOR = 10**11
MAX_QTY_MILLI = 10**9


def _clean(text: str) -> str:
    t = (text or "").strip().lower()
    for w in ("тенге", "kzt", "тг", "₸"):
        t = t.replace(w, "")
    return t.replace("\u00a0", "").replace(" ", "").replace(",", ".")


def _to_decimal(text: str, err: str) -> Decimal:
    try:
        d = Decimal(_clean(text))
    except InvalidOperation:
        raise ServiceError(err)
    if not d.is_finite() or abs(d) > 10**9:
        raise ServiceError(err)
    return d


def parse_money(text: str) -> int:
    d = _to_decimal(text, "Не понял сумму. Введите число, например 1500 или 1500.50")
    if d != d.quantize(Decimal("0.01")):
        raise ServiceError("Не больше двух знаков после запятой.")
    minor = int(d * 100)
    if minor <= 0:
        raise ServiceError("Сумма должна быть больше нуля.")
    if minor > MAX_MINOR:
        raise ServiceError("Слишком большая сумма.")
    return minor


def parse_qty(text: str) -> int:
    d = _to_decimal(text, "Не понял количество. Введите число, например 12 или 1.5")
    if d != d.quantize(Decimal("0.001")):
        raise ServiceError("Не больше трёх знаков после запятой.")
    milli = int(d * 1000)
    if milli <= 0:
        raise ServiceError("Количество должно быть больше нуля.")
    if milli > MAX_QTY_MILLI:
        raise ServiceError("Слишком большое количество.")
    return milli


def fmt_money(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    tenge, tiyn = divmod(abs(minor), 100)
    s = f"{tenge:,}".replace(",", " ")
    if tiyn:
        s += f",{tiyn:02d}"
    return f"{sign}{s} ₸"


def fmt_qty(milli: int) -> str:
    if milli % 1000 == 0:
        return str(milli // 1000)
    return f"{Decimal(milli) / 1000:f}".rstrip("0").rstrip(".")
