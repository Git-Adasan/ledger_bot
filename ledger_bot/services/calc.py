from dataclasses import dataclass

from ..errors import ServiceError


@dataclass(frozen=True)
class CalcType:
    key: str
    title: str
    unit: str
    fraction: bool  # допускается дробное количество


CALC_TYPES = {
    "unit": CalcType("unit", "За единицу", "шт", False),
    "sheet": CalcType("sheet", "За лист", "лист", False),
    "copy": CalcType("copy", "За экземпляр", "экз.", False),
    "hour": CalcType("hour", "За час", "ч", True),
    "fixed": CalcType("fixed", "Фиксированная стоимость", "усл.", False),
    "custom": CalcType("custom", "Произвольное количество × цена", "ед.", True),
}


def line_total(price_minor: int, qty_milli: int) -> int:
    """Сумма позиции в тиынах, округление половины вверх."""
    return (price_minor * qty_milli + 500) // 1000


def validate_qty(calc_type: str, qty_milli: int) -> None:
    ct = CALC_TYPES[calc_type]
    if ct.key == "fixed" and qty_milli != 1000:
        raise ServiceError("Для фиксированной стоимости количество всегда 1.")
    if not ct.fraction and qty_milli % 1000:
        raise ServiceError(f"Для «{ct.title}» нужно целое количество.")
