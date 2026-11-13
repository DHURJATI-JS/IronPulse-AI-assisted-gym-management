"""Gym shop: products (supplements, apparel, accessories, wearables ...), images, cart, wishlist,
orders, refunds/returns, coupons, reviews and inventory movements.

Orders and refunds are financial records: their party foreign keys use SET NULL and keep snapshot
labels, so they survive the 30-day account purge (anonymised)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import EMBEDDING_DIM, CreatedMixin, IdMixin, TimestampMixin, enum_col, fk

if TYPE_CHECKING:
    from app.models.gyms import Branch, Gym
    from app.models.users import User


class ProductCategory(StrEnum):
    SUPPLEMENTS = "supplements"  # whey, creatine, BCAA, pre-workout ...
    NUTRITION = "nutrition"  # bars, shakes, snacks
    APPAREL = "apparel"
    ACCESSORIES = "accessories"  # gloves, belts, straps, shakers, bags
    WEARABLES = "wearables"  # watches, heart-rate straps
    EQUIPMENT = "equipment"  # bands, mats, dumbbells
    OTHER = "other"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PROCESSING = "processing"
    READY_FOR_PICKUP = "ready_for_pickup"
    SHIPPED = "shipped"
    COMPLETED = "completed"  # picked up / delivered
    CANCELLED = "cancelled"
    PAYMENT_FAILED = "payment_failed"
    REFUND_REQUESTED = "refund_requested"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class RefundStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class Product(IdMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("gym_id", "slug"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("compare_at_price IS NULL OR compare_at_price >= price", name="compare_at_price"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
        CheckConstraint("tax_rate BETWEEN 0 AND 50", name="tax_rate_range"),
        # shop search: ILIKE '%q%' on name / brand (pg_trgm) and nearest products by embedding (pgvector HNSW,
        # used by ORDER BY embedding <=> :q LIMIT k)
        Index("ix_products_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("ix_products_brand_trgm", "brand", postgresql_using="gin", postgresql_ops={"brand": "gin_trgm_ops"}),
        Index("ix_products_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        # catalogue orders read in index order - never a sort of every matching product: "Featured" (the default: in
        # stock first, then newest), "Top rated" (the stored review summary), "Newest" and both price orders (one index,
        # scanned forwards or backwards). The predicate is the catalogue's own (services/shop.py search_products).
        Index("ix_products_catalogue_featured", text("in_stock DESC"), text("created_at DESC"), text("id DESC"),
              postgresql_where=text("is_active IS true")),
        Index("ix_products_catalogue_rating", text("rating_avg DESC NULLS LAST"), text("review_count DESC"), "id",
              postgresql_where=text("is_active IS true")),
        Index("ix_products_catalogue_newest", text("created_at DESC"), text("id DESC"),
              postgresql_where=text("is_active IS true")),
        Index("ix_products_catalogue_price", "price", "id", postgresql_where=text("is_active IS true")),
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(180))
    category: Mapped[ProductCategory] = enum_col(ProductCategory, "category", default=ProductCategory.OTHER)
    brand: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    compare_at_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), server_default="0")  # % (e.g. GST 18)
    stock: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    sku: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), index=True)
    is_supplement: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    age_restricted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # 18+
    allergens: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    caution_tags: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # safety tags
    ingredients: Mapped[str | None] = mapped_column(Text)
    nutrition_facts: Mapped[dict | None] = mapped_column(JSONB)
    fulfillment: Mapped[str] = mapped_column(String(10), default="both", server_default="both")  # pickup|delivery|both
    pickup_branch_id: Mapped[int | None] = fk("branches.id", "SET NULL")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # ``stock > 0``, computed and stored by PostgreSQL (whoever writes the stock: ORM, raw SQL, COPY) so the catalogue's
    # default order can be read from ix_products_catalogue_featured. SQL only: never loaded with a Product (raiseload).
    in_stock: Mapped[bool] = mapped_column(Boolean, Computed("stock > 0", persisted=True), deferred=True,
                                           deferred_raiseload=True)
    # summary of the VISIBLE reviews (rating_avg NULL = none), rewritten in the same transaction as every review insert,
    # edit, deletion or moderation (app/services/shop.py: refresh_product_ratings + its flush hook; reconciled hourly)
    rating_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    gym: Mapped[Gym] = relationship(lazy="raise")
    pickup_branch: Mapped[Branch | None] = relationship(lazy="raise")
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product", lazy="raise", passive_deletes=True, order_by="ProductImage.sort_order"
    )


class ProductImage(IdMixin, CreatedMixin, Base):
    __tablename__ = "product_images"

    product_id: Mapped[int] = fk("products.id", "CASCADE")
    image_key: Mapped[str] = mapped_column(String(255))
    alt: Mapped[str | None] = mapped_column(String(160))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # the gym's storage quota

    product: Mapped[Product] = relationship(back_populates="images", lazy="raise")


class CartItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id"), CheckConstraint("quantity BETWEEN 1 AND 20", name="qty_range"))

    user_id: Mapped[int] = fk("users.id", "CASCADE")
    product_id: Mapped[int] = fk("products.id", "CASCADE")
    quantity: Mapped[int] = mapped_column(SmallInteger, default=1)

    product: Mapped[Product] = relationship(lazy="raise")


class WishlistItem(IdMixin, CreatedMixin, Base):
    __tablename__ = "wishlist_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)

    user_id: Mapped[int] = fk("users.id", "CASCADE")
    product_id: Mapped[int] = fk("products.id", "CASCADE")

    product: Mapped[Product] = relationship(lazy="raise")


class Coupon(IdMixin, TimestampMixin, Base):
    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("gym_id", "code"),
        CheckConstraint("(percent_off IS NULL) <> (amount_off IS NULL)", name="one_discount_kind"),
        CheckConstraint("percent_off IS NULL OR percent_off BETWEEN 1 AND 90", name="percent_range"),
        CheckConstraint("amount_off IS NULL OR amount_off > 0", name="amount_positive"),
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    code: Mapped[str] = mapped_column(String(32))
    percent_off: Mapped[int | None] = mapped_column(SmallInteger)
    amount_off: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_order_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ShopOrder(IdMixin, TimestampMixin, Base):
    __tablename__ = "shop_orders"
    __table_args__ = (
        CheckConstraint("total >= 0 AND subtotal >= 0", name="totals_non_negative"),
        CheckConstraint("fulfillment IN ('pickup', 'delivery')", name="fulfillment_values"),
        # the orders_per_member_day cap (services/shop.py place_order): one member's orders of the last 24 hours - an
        # index range of at most the cap's worth of rows, never their whole order history
        Index("ix_shop_orders_customer_created", "customer_id", "created_at"),
        # the units held by open checkouts (services/shop.py held_units): a range of the orders still holding stock -
        # the hold is cleared when the order is paid, cancelled, failed or expired, so the index stays small
        Index("ix_shop_orders_stock_hold", "stock_held_until", postgresql_where=text("stock_held_until IS NOT NULL")),
    )

    order_number: Mapped[str] = mapped_column(String(24), unique=True)
    gym_id: Mapped[int | None] = fk("gyms.id", "SET NULL")
    customer_id: Mapped[int | None] = fk("users.id", "SET NULL")
    customer_label: Mapped[str] = mapped_column(String(160))  # snapshot (anonymised on purge)
    status: Mapped[OrderStatus] = enum_col(OrderStatus, "status", default=OrderStatus.PENDING_PAYMENT, index=True)
    fulfillment: Mapped[str] = mapped_column(String(10), default="pickup")
    pickup_branch_id: Mapped[int | None] = fk("branches.id", "SET NULL")
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    refunded_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    currency: Mapped[str] = mapped_column(String(3))
    coupon_code: Mapped[str | None] = mapped_column(String(32))
    payment_provider: Mapped[str | None] = mapped_column(String(20))  # cashfree | test | cash
    provider_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_session_id: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_ip: Mapped[str | None] = mapped_column(String(45))
    # an unpaid order keeps its units for the buyer until then (services/shop.py STOCK_HOLD_MINUTES): other checkouts
    # can't take them, so the last units are never charged twice and refunded. NULL = holds nothing
    stock_held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    gym: Mapped[Gym | None] = relationship(lazy="raise")
    customer: Mapped[User | None] = relationship(lazy="raise")
    pickup_branch: Mapped[Branch | None] = relationship(lazy="raise")
    items: Mapped[list[ShopOrderItem]] = relationship(
        back_populates="order", lazy="raise", passive_deletes=True, order_by="ShopOrderItem.id"
    )
    refunds: Mapped[list[Refund]] = relationship(back_populates="order", lazy="raise", passive_deletes=True)


class ShopOrderItem(IdMixin, Base):
    __tablename__ = "shop_order_items"
    __table_args__ = (
        CheckConstraint("quantity BETWEEN 1 AND 20", name="qty_range"),
        CheckConstraint("refunded_quantity BETWEEN 0 AND quantity", name="refunded_qty_range"),
    )

    order_id: Mapped[int] = fk("shop_orders.id", "CASCADE")
    product_id: Mapped[int | None] = fk("products.id", "SET NULL")
    product_name: Mapped[str] = mapped_column(String(160))  # snapshot
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    quantity: Mapped[int] = mapped_column(SmallInteger)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    refunded_quantity: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    order: Mapped[ShopOrder] = relationship(back_populates="items", lazy="raise")
    product: Mapped[Product | None] = relationship(lazy="raise")


class Refund(IdMixin, TimestampMixin, Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    order_id: Mapped[int] = fk("shop_orders.id", "CASCADE")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reason: Mapped[str] = mapped_column(Text)
    items: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # [{order_item_id, quantity}]
    restock: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    status: Mapped[RefundStatus] = enum_col(RefundStatus, "status", default=RefundStatus.REQUESTED, index=True)
    requested_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    decided_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    decision_note: Mapped[str | None] = mapped_column(Text)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64))
    transaction_id: Mapped[int | None] = fk("transactions.id", "SET NULL")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[ShopOrder] = relationship(back_populates="refunds", lazy="raise")


class ProductReview(IdMixin, TimestampMixin, Base):
    """Only verified purchasers (a completed order containing the product) may review."""

    __tablename__ = "product_reviews"
    __table_args__ = (UniqueConstraint("product_id", "user_id"), CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"))

    product_id: Mapped[int] = fk("products.id", "CASCADE")
    user_id: Mapped[int] = fk("users.id", "CASCADE")
    rating: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(Text)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # owner moderation

    user: Mapped[User] = relationship(lazy="raise")


class InventoryMovement(IdMixin, CreatedMixin, Base):
    __tablename__ = "inventory_movements"

    product_id: Mapped[int] = fk("products.id", "CASCADE")
    change: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(30))  # restock | sale | cancel | refund_restock | adjustment
    order_id: Mapped[int | None] = fk("shop_orders.id", "SET NULL")
    actor_id: Mapped[int | None] = fk("users.id", "SET NULL")
    note: Mapped[str | None] = mapped_column(String(255))
