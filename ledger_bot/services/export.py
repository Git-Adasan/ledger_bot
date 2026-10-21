import csv
import io
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (Adjustment, Client, Order, OrderItem, Payment, PaymentAllocation,
                         Service, Transaction)

TABLES = {
    "clients.csv": Client, "services.csv": Service, "orders.csv": Order,
    "order_items.csv": OrderItem, "payments.csv": Payment,
    "payment_allocations.csv": PaymentAllocation, "adjustments.csv": Adjustment,
    "transactions.csv": Transaction,
}


def _safe(v):
    """Защита от формул в Excel (=, +, -, @ в начале текста)."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def export_zip(s: Session) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, model in TABLES.items():
            cols = [c.name for c in model.__table__.columns]
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(cols)
            for row in s.scalars(select(model).order_by(model.id)):
                w.writerow([_safe(getattr(row, c)) for c in cols])
            z.writestr(fname, "\ufeff" + out.getvalue())  # BOM: Excel читает UTF-8
        z.writestr("README.txt", "Суммы *_minor указаны в тиынах (100 = 1 ₸). "
                                 "qty_milli — количество × 1000.\n")
    return buf.getvalue()
