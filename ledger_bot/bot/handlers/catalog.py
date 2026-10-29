from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...db.session import session_scope
from ...money import parse_money
from ...services import catalog as cat
from ...services.calc import CALC_TYPES
from ..common import show
from ..format import price_label, service_text
from ..keyboards import CANCEL, ikb

router = Router()


class CatFlow(StatesGroup):
    cat_name = State()
    rename_cat = State()
    svc_name = State()
    svc_unit = State()
    svc_price = State()
    edit = State()
    var_name = State()
    var_price = State()


HOME = ikb([[("Категории и услуги", "cg:cats")], [("+ Услуга", "cg:new"), ("+ Категория", "cg:newcat")],
            [("Архив услуг", "cg:arch")]])


async def start_catalog(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Каталог услуг", reply_markup=HOME)


@router.callback_query(F.data == "cg:home")
async def home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Каталог услуг", HOME)


@router.callback_query(F.data == "cg:cats")
async def categories(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = [[(f"{c.name} ({len(cat.list_services(s, category_id=c.id))})", f"cg:cat:{c.id}")]
                for c in cat.list_categories(s)]
    await show(cb, "Категории:" if rows else "Категорий нет.", ikb(rows + [[("Назад", "cg:home")]]))


@router.callback_query(F.data.startswith("cg:cat:"))
async def category(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    with session_scope() as s:
        c = s.get(cat.Category, cid)
        rows = [[(("⏸ " if not sv.is_active else "") + f"{sv.name} — {price_label(sv)}", f"cg:svc:{sv.id}")]
                for sv in cat.list_services(s, category_id=cid)]
        name = c.name
    await show(cb, f"Категория «{name}»", ikb(rows + [[("Переименовать", f"cg:rcat:{cid}")],
                                                       [("Назад", "cg:cats")]]))


@router.callback_query(F.data == "cg:arch")
async def archive(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = [[(f"{sv.name} — {price_label(sv)}", f"cg:svc:{sv.id}")] for sv in cat.list_services(s, archived=True)]
    await show(cb, "Архив:" if rows else "Архив пуст.", ikb(rows + [[("Назад", "cg:home")]]))


# ---------- карточка услуги ----------
async def show_service(event, sid):
    with session_scope() as s:
        sv = cat.get_service(s, sid)
        text, arch, active, cid = service_text(sv), sv.is_archived, sv.is_active, sv.category_id
    if arch:
        rows = [[("Восстановить", f"cg:rs:{sid}")], [("Назад", "cg:arch")]]
    else:
        rows = [[("Название", f"cg:ed:name:{sid}"), ("Цена", f"cg:ed:price:{sid}")],
                [("Единица", f"cg:ed:unit:{sid}"), ("Способ расчёта", f"cg:calc:{sid}")],
                [("Двусторонняя: вкл/выкл", f"cg:pp:{sid}")],
                [("Категория", f"cg:mv:{sid}"), ("Варианты", f"cg:vr:{sid}")],
                [("Отключить" if active else "Включить", f"cg:tg:{sid}"), ("В архив", f"cg:ar:{sid}")],
                [("Назад", f"cg:cat:{cid}")]]
    await show(event, text, ikb(rows))


@router.callback_query(F.data.startswith("cg:svc:"))
async def service_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_service(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^cg:(tg|ar|rs):\d+$"))
async def service_state(cb: CallbackQuery, state: FSMContext):
    _, act, sid = cb.data.split(":")
    sid = int(sid)
    with session_scope() as s:
        if act == "tg":
            cat.set_active(s, sid, not cat.get_service(s, sid).is_active)
        elif act == "ar":
            cat.archive_service(s, sid)
        else:
            cat.restore_service(s, sid)
    await show_service(cb, sid)


@router.callback_query(F.data.startswith("cg:pp:"))
async def toggle_duplex(cb: CallbackQuery, state: FSMContext):
    """Вкл: при заказе вводят страницы, бот делит на 2 и умножает на цену листа."""
    sid = int(cb.data.split(":")[2])
    with session_scope() as s:
        cur = cat.get_service(s, sid).pages_per_sheet
        cat.update_service(s, sid, pages_per_sheet=1 if cur > 1 else 2)
    await show_service(cb, sid)


@router.callback_query(F.data.startswith("cg:calc:"))
async def calc_choose(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await show(cb, "Способ расчёта (старые заказы не изменятся):",
               ikb([[(ct.title, f"cg:ct:{sid}:{k}")] for k, ct in CALC_TYPES.items()] + [[("Назад", f"cg:svc:{sid}")]]))


@router.callback_query(F.data.startswith("cg:ct:"))
async def calc_set(cb: CallbackQuery, state: FSMContext):
    _, _, sid, key = cb.data.split(":")
    with session_scope() as s:
        cat.update_service(s, int(sid), calc_type=key)
    await show_service(cb, int(sid))


@router.callback_query(F.data.startswith("cg:mv:"))
async def move_choose(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    with session_scope() as s:
        rows = [[(c.name, f"cg:mt:{sid}:{c.id}")] for c in cat.list_categories(s)]
    await show(cb, "Переместить в категорию:", ikb(rows + [[("Назад", f"cg:svc:{sid}")]]))


@router.callback_query(F.data.startswith("cg:mt:"))
async def move_do(cb: CallbackQuery, state: FSMContext):
    _, _, sid, cid = cb.data.split(":")
    with session_scope() as s:
        cat.move_service(s, int(sid), int(cid))
    await show_service(cb, int(sid))


# ---------- редактирование полей ----------
PROMPTS = {"name": "Новое название:", "price": "Новая цена в ₸:", "unit": "Новая единица измерения (например: лист):",
           "vname": "Новое название варианта:", "vprice": "Новая цена варианта в ₸:"}


@router.callback_query(F.data.startswith("cg:ed:") | F.data.startswith("cg:ve:"))
async def edit_ask(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    field = parts[2] if parts[1] == "ed" else "v" + parts[2]
    await state.set_state(CatFlow.edit)
    await state.update_data(field=field, id=int(parts[3]))
    await show(cb, PROMPTS[field], ikb([CANCEL]))


@router.message(CatFlow.edit, F.text)
async def edit_do(message: Message, state: FSMContext):
    d = await state.get_data()
    f, i = d["field"], d["id"]
    with session_scope() as s:
        if f == "name":
            cat.update_service(s, i, name=message.text)
        elif f == "unit":
            cat.update_service(s, i, unit=message.text)
        elif f == "price":
            cat.update_service(s, i, price_minor=parse_money(message.text))
        elif f == "vname":
            cat.update_variant(s, i, name=message.text)
        else:
            cat.update_variant(s, i, price_minor=parse_money(message.text))
        sid = i if not f.startswith("v") else cat.get_variant(s, i).service_id
    await state.clear()
    if f.startswith("v"):
        await show_variant(message, i)
    else:
        await show_service(message, sid)


# ---------- варианты ----------
@router.callback_query(F.data.startswith("cg:vr:"))
async def variants(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    with session_scope() as s:
        sv = cat.get_service(s, sid)
        rows = [[(("" if v.is_active else "⏸ ") + f"{v.name} — {price_label(sv, v.price_minor)}", f"cg:vv:{v.id}")]
                for v in sv.variants]
    await show(cb, "Варианты (например, тип печати). Если есть активные варианты, при заказе выбирается один из них.",
               ikb(rows + [[("+ Вариант", f"cg:vnew:{sid}")], [("Назад", f"cg:svc:{sid}")]]))


async def show_variant(event, vid):
    with session_scope() as s:
        v = cat.get_variant(s, vid)
        text = f"{v.name}\nЦена: {price_label(v.service, v.price_minor)}\nСтатус: {'включён' if v.is_active else 'отключён'}"
        if v.pages_per_sheet > 1:
            text += f"\nДвусторонняя: вводятся страницы, {v.pages_per_sheet} стр. на лист"
        sid, active = v.service_id, v.is_active
    await show(event, text, ikb([[("Название", f"cg:ve:name:{vid}"), ("Цена", f"cg:ve:price:{vid}")],
                                 [("Двусторонняя: вкл/выкл", f"cg:vp:{vid}")],
                                 [("Отключить" if active else "Включить", f"cg:vt:{vid}")],
                                 [("Назад", f"cg:vr:{sid}")]]))


@router.callback_query(F.data.startswith("cg:vv:"))
async def variant_card(cb: CallbackQuery, state: FSMContext):
    await show_variant(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("cg:vp:"))
async def variant_duplex(cb: CallbackQuery, state: FSMContext):
    vid = int(cb.data.split(":")[2])
    with session_scope() as s:
        cur = cat.get_variant(s, vid).pages_per_sheet
        cat.update_variant(s, vid, pages_per_sheet=1 if cur > 1 else 2)
    await show_variant(cb, vid)


@router.callback_query(F.data.startswith("cg:vt:"))
async def variant_toggle(cb: CallbackQuery, state: FSMContext):
    vid = int(cb.data.split(":")[2])
    with session_scope() as s:
        cat.update_variant(s, vid, active=not cat.get_variant(s, vid).is_active)
    await show_variant(cb, vid)


@router.callback_query(F.data.startswith("cg:vnew:"))
async def variant_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CatFlow.var_name)
    await state.update_data(sid=int(cb.data.split(":")[2]))
    await show(cb, "Название варианта:", ikb([CANCEL]))


@router.message(CatFlow.var_name, F.text)
async def variant_name(message: Message, state: FSMContext):
    if not message.text.strip() or len(message.text.strip()) > 80:
        await message.answer("Название: от 1 до 80 символов.")
        return
    await state.update_data(vname=message.text.strip())
    await state.set_state(CatFlow.var_price)
    await message.answer("Цена варианта в ₸:", reply_markup=ikb([CANCEL]))


@router.message(CatFlow.var_price, F.text)
async def variant_price(message: Message, state: FSMContext):
    d = await state.get_data()
    with session_scope() as s:
        v = cat.add_variant(s, d["sid"], d["vname"], parse_money(message.text))
        vid = v.id
    await state.clear()
    await show_variant(message, vid)


# ---------- создание категории ----------
@router.callback_query(F.data == "cg:newcat")
async def newcat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CatFlow.cat_name)
    await state.update_data(after=False)
    await show(cb, "Название категории:", ikb([CANCEL]))


@router.callback_query(F.data.startswith("cg:rcat:"))
async def rename_cat_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CatFlow.rename_cat)
    await state.update_data(id=int(cb.data.split(":")[2]))
    await show(cb, "Новое название категории:", ikb([CANCEL]))


@router.message(CatFlow.rename_cat, F.text)
async def rename_cat_do(message: Message, state: FSMContext):
    d = await state.get_data()
    with session_scope() as s:
        cat.rename_category(s, d["id"], message.text)
    await state.clear()
    await message.answer("Переименовано.", reply_markup=HOME)


@router.message(CatFlow.cat_name, F.text)
async def cat_name(message: Message, state: FSMContext):
    d = await state.get_data()
    with session_scope() as s:
        c = cat.create_category(s, message.text)
        cid = c.id
    if d.get("after"):
        await state.update_data(cat=cid, after=False)
        await _ask_type(message)
    else:
        await state.clear()
        await message.answer("Категория создана.", reply_markup=HOME)


# ---------- создание услуги ----------
@router.callback_query(F.data == "cg:new")
async def new_service(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(CatFlow.svc_name)
    await show(cb, "Название новой услуги:", ikb([CANCEL]))


@router.message(CatFlow.svc_name, F.text)
async def svc_name(message: Message, state: FSMContext):
    if not message.text.strip() or len(message.text.strip()) > 80:
        await message.answer("Название: от 1 до 80 символов.")
        return
    await state.update_data(name=message.text.strip())
    with session_scope() as s:
        rows = [[(c.name, f"cg:nc:{c.id}")] for c in cat.list_categories(s)]
    await message.answer("Категория:", reply_markup=ikb(rows + [[("+ Новая категория", "cg:nc:new")], CANCEL]))


@router.callback_query(F.data.startswith("cg:nc:"))
async def svc_category(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":")[2]
    if val == "new":
        await state.set_state(CatFlow.cat_name)
        await state.update_data(after=True)
        await show(cb, "Название новой категории:", ikb([CANCEL]))
        return
    await state.update_data(cat=int(val))
    await _ask_type(cb)


async def _ask_type(event):
    await show(event, "Способ расчёта:", ikb([[(ct.title, f"cg:nt:{k}")] for k, ct in CALC_TYPES.items()] + [CANCEL]))


@router.callback_query(F.data.startswith("cg:nt:"))
async def svc_type(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(ct=key, unit=CALC_TYPES[key].unit)
    if key == "custom":
        await state.set_state(CatFlow.svc_unit)
        await show(cb, "Единица измерения (например: страница, кг):", ikb([CANCEL]))
    else:
        await _ask_price(cb, state)


@router.message(CatFlow.svc_unit, F.text)
async def svc_unit(message: Message, state: FSMContext):
    if not message.text.strip() or len(message.text.strip()) > 24:
        await message.answer("Единица: от 1 до 24 символов.")
        return
    await state.update_data(unit=message.text.strip())
    await _ask_price(message, state)


async def _ask_price(event, state):
    d = await state.get_data()
    await state.set_state(CatFlow.svc_price)
    label = "Стоимость в ₸:" if d["ct"] == "fixed" else f"Цена за 1 {d['unit']} в ₸:"
    await show(event, label, ikb([CANCEL]))


@router.message(CatFlow.svc_price, F.text)
async def svc_price(message: Message, state: FSMContext):
    d = await state.get_data()
    with session_scope() as s:
        sv = cat.create_service(s, d["name"], d["cat"], d["ct"], d["unit"], parse_money(message.text))
        sid = sv.id
    await state.clear()
    await message.answer("Услуга создана.")
    await show_service(message, sid)
