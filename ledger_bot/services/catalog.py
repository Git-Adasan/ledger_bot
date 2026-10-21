from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..db.models import Category, Service, ServiceVariant
from ..errors import ServiceError
from .calc import CALC_TYPES


def _name(text: str) -> str:
    t = (text or "").strip()
    if not t or len(t) > 80:
        raise ServiceError("Название должно быть от 1 до 80 символов.")
    return t


def create_category(s: Session, name: str) -> Category:
    name = _name(name)
    if any(c.name.casefold() == name.casefold() for c in list_categories(s)):
        raise ServiceError("Такая категория уже есть.")
    c = Category(name=name)
    s.add(c)
    s.flush()
    return c


def rename_category(s: Session, category_id: int, name: str) -> Category:
    c = s.get(Category, category_id)
    if not c:
        raise ServiceError("Категория не найдена.")
    name = _name(name)
    if any(x.id != c.id and x.name.casefold() == name.casefold() for x in list_categories(s)):
        raise ServiceError("Такая категория уже есть.")
    c.name = name
    return c


def list_categories(s: Session) -> list[Category]:
    return list(s.scalars(select(Category).order_by(Category.name)))


def categories_with_services(s: Session) -> list[Category]:
    """Категории, где есть хотя бы одна доступная для заказа услуга."""
    ids = set(s.scalars(select(Service.category_id).where(
        Service.is_active, ~Service.is_archived)))
    return [c for c in list_categories(s) if c.id in ids]


def get_service(s: Session, service_id: int) -> Service:
    sv = s.get(Service, service_id)
    if not sv:
        raise ServiceError("Услуга не найдена.")
    return sv


def create_service(s: Session, name, category_id, calc_type, unit, price_minor) -> Service:
    if calc_type not in CALC_TYPES:
        raise ServiceError("Неизвестный способ расчёта.")
    if not s.get(Category, category_id):
        raise ServiceError("Категория не найдена.")
    if price_minor <= 0:
        raise ServiceError("Цена должна быть больше нуля.")
    unit = (unit or "").strip()
    if not unit or len(unit) > 24:
        raise ServiceError("Единица измерения: от 1 до 24 символов.")
    sv = Service(name=_name(name), category_id=category_id, calc_type=calc_type,
                 unit=unit, price_minor=price_minor)
    s.add(sv)
    s.flush()
    return sv


def update_service(s: Session, service_id: int, *, name=None, unit=None,
                   price_minor=None, calc_type=None) -> Service:
    sv = get_service(s, service_id)
    if name is not None:
        sv.name = _name(name)
    if unit is not None:
        unit = unit.strip()
        if not unit or len(unit) > 24:
            raise ServiceError("Единица измерения: от 1 до 24 символов.")
        sv.unit = unit
    if price_minor is not None:
        if price_minor <= 0:
            raise ServiceError("Цена должна быть больше нуля.")
        sv.price_minor = price_minor
    if calc_type is not None:
        if calc_type not in CALC_TYPES:
            raise ServiceError("Неизвестный способ расчёта.")
        sv.calc_type = calc_type
    return sv


def move_service(s: Session, service_id: int, category_id: int) -> Service:
    if not s.get(Category, category_id):
        raise ServiceError("Категория не найдена.")
    sv = get_service(s, service_id)
    sv.category_id = category_id
    return sv


def set_active(s: Session, service_id: int, active: bool) -> Service:
    sv = get_service(s, service_id)
    sv.is_active = active
    return sv


def archive_service(s: Session, service_id: int) -> Service:
    sv = get_service(s, service_id)
    sv.is_archived = True
    return sv


def restore_service(s: Session, service_id: int) -> Service:
    sv = get_service(s, service_id)
    sv.is_archived = False
    sv.is_active = True
    return sv


def list_services(s: Session, category_id=None, archived=False, selectable=False) -> list[Service]:
    q = select(Service).where(Service.is_archived == archived)
    if category_id is not None:
        q = q.where(Service.category_id == category_id)
    if selectable:
        q = q.where(Service.is_active)
    return list(s.scalars(q.order_by(Service.name)))


def active_variants(sv: Service) -> list[ServiceVariant]:
    return [v for v in sv.variants if v.is_active]


def add_variant(s: Session, service_id: int, name: str, price_minor: int) -> ServiceVariant:
    sv = get_service(s, service_id)
    if price_minor <= 0:
        raise ServiceError("Цена должна быть больше нуля.")
    v = ServiceVariant(service_id=sv.id, name=_name(name), price_minor=price_minor)
    s.add(v)
    s.flush()
    return v


def get_variant(s: Session, variant_id: int) -> ServiceVariant:
    v = s.get(ServiceVariant, variant_id)
    if not v:
        raise ServiceError("Вариант не найден.")
    return v


def update_variant(s: Session, variant_id: int, *, name=None, price_minor=None, active=None):
    v = get_variant(s, variant_id)
    if name is not None:
        v.name = _name(name)
    if price_minor is not None:
        if price_minor <= 0:
            raise ServiceError("Цена должна быть больше нуля.")
        v.price_minor = price_minor
    if active is not None:
        v.is_active = active
    return v


def seed_defaults(s: Session) -> None:
    """Начальные данные — только в пустую базу. Всё меняется через бота."""
    if s.scalar(select(func.count(Category.id))) or s.scalar(select(func.count(Service.id))):
        return
    prt = create_category(s, "Печать")
    con = create_category(s, "Конспекты")
    alb = create_category(s, "Альбомы")
    create_service(s, "Односторонняя печать", prt.id, "sheet", "лист", 3500)
    create_service(s, "Двухсторонняя печать", prt.id, "sheet", "лист", 5500)
    create_service(s, "Конспект", con.id, "copy", "экз.", 20000)
    a = create_service(s, "Альбом по гистологии", alb.id, "sheet", "лист", 3500)
    add_variant(s, a.id, "Односторонняя печать", 3500)
    add_variant(s, a.id, "Двухсторонняя печать", 5500)
