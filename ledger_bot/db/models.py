from datetime import date, datetime
from typing import Optional

from sqlalchemy import (CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..timeutil import now


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    calc_type: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(24))
    price_minor: Mapped[int]
    # 2 = двусторонняя печать: при заказе вводят страницы, бот делит на 2 (вверх) и считает листы
    pages_per_sheet: Mapped[int] = mapped_column(default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    category: Mapped[Category] = relationship()
    variants: Mapped[list["ServiceVariant"]] = relationship(
        back_populates="service", order_by="ServiceVariant.id")


class ServiceVariant(Base):
    __tablename__ = "service_variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    name: Mapped[str] = mapped_column(String(80))
    price_minor: Mapped[int]
    pages_per_sheet: Mapped[int] = mapped_column(default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(default=True)
    service: Mapped[Service] = relationship(back_populates="variants")


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[Optional[str]] = mapped_column(Text)
    is_archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_minor >= 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | cancelled
    total_minor: Mapped[int]
    note: Mapped[Optional[str]] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    items: Mapped[list["OrderItem"]] = relationship(
        cascade="all, delete-orphan", order_by="OrderItem.id")


class OrderItem(Base):
    """Снимок позиции: название, цена и сумма на момент заказа."""
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    service_id: Mapped[Optional[int]] = mapped_column(ForeignKey("services.id"))
    variant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("service_variants.id"))
    name: Mapped[str] = mapped_column(String(200))
    qty_milli: Mapped[int]
    unit: Mapped[str] = mapped_column(String(24))
    price_minor: Mapped[int]
    total_minor: Mapped[int]


class Payment(Base):
    """kind: payment — получено от клиента; refund — возвращено клиенту."""
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_minor > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="payment")
    amount_minor: Mapped[int]
    method: Mapped[Optional[str]] = mapped_column(String(24))
    note: Mapped[Optional[str]] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class PaymentAllocation(Base):
    """Какая часть платежа закрывает какой заказ. Не удаляется — только released_at."""
    __tablename__ = "payment_allocations"
    __table_args__ = (CheckConstraint("amount_minor > 0"),
                      Index("ix_alloc_order", "order_id", "released_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    amount_minor: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Adjustment(Base):
    """Списание долга по заказу (не является полученными деньгами)."""
    __tablename__ = "adjustments"
    __table_args__ = (CheckConstraint("amount_minor > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"))
    amount_minor: Mapped[int]
    reason: Mapped[Optional[str]] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Transaction(Base):
    """Доход/расход вне заказов. Аннулирование — voided_at, запись не удаляется."""
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("amount_minor > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # income | expense
    amount_minor: Mapped[int]
    date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(80))
    description: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Attachment(Base):
    """Файл заказа (kind=file) или чек оплаты (kind=receipt).

    Сам файл на сервере НЕ хранится: в базе только ссылка Telegram (file_id) и имя.
    Через N дней ссылка стирается (file_id = NULL, purged_at заполняется), а имя и дата остаются.
    """
    __tablename__ = "attachments"
    __table_args__ = (CheckConstraint("(order_id IS NULL) != (payment_id IS NULL)"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payments.id"), index=True)
    media: Mapped[str] = mapped_column(String(8))  # document | photo
    file_id: Mapped[Optional[str]] = mapped_column(Text)
    unique_id: Mapped[Optional[str]] = mapped_column(String(64))
    file_name: Mapped[Optional[str]] = mapped_column(String(200))
    size: Mapped[Optional[int]]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    @property
    def kind(self) -> str:
        return "file" if self.order_id is not None else "receipt"
