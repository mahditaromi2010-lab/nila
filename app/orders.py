"""Cart and order logic, kept out of the views so both shop and admin share it."""
from __future__ import annotations

import secrets
from datetime import timedelta

from flask import session

from . import settings
from .extensions import db
from .models import STOCK_HELD_STATUSES, Book, Coupon, Order, OrderItem, Payment
from .utils import utcnow

CART_KEY = "cart"


class OrderError(ValueError):
    pass


# ------------------------------------------------------------------ cart (session)

def cart_raw() -> dict[str, int]:
    raw = session.get(CART_KEY) or {}
    return {str(k): int(v) for k, v in raw.items() if str(k).isdigit() and int(v) > 0}


def cart_set(book_id: int, qty: int) -> None:
    raw = cart_raw()
    if qty <= 0:
        raw.pop(str(book_id), None)
    else:
        raw[str(book_id)] = qty
    session[CART_KEY] = raw
    session.modified = True


def cart_add(book: Book, qty: int = 1) -> int:
    raw = cart_raw()
    new_qty = min(raw.get(str(book.id), 0) + max(1, qty), book.max_qty())
    if new_qty <= 0:
        raise OrderError("این کتاب فعلاً موجود نیست.")
    cart_set(book.id, new_qty)
    return new_qty


def cart_clear() -> None:
    session.pop(CART_KEY, None)
    session.pop("coupon", None)


def cart_count() -> int:
    return sum(cart_raw().values())


def cart_lines() -> list[dict]:
    raw = cart_raw()
    if not raw:
        return []
    books = {b.id: b for b in Book.query.filter(Book.id.in_([int(k) for k in raw])).all()}
    lines, changed = [], False
    for bid, qty in raw.items():
        b = books.get(int(bid))
        if b is None or not b.is_active or not b.in_stock:
            changed = True
            continue
        q = min(qty, b.max_qty())
        if q != qty:
            changed = True
        lines.append({"book": b, "qty": q, "total": q * b.price})
    if changed:
        session[CART_KEY] = {str(l["book"].id): l["qty"] for l in lines}
    return lines


def find_coupon(code: str) -> Coupon | None:
    code = (code or "").strip().upper()
    return Coupon.query.filter_by(code=code).first() if code else None


def totals(lines: list[dict], coupon_code: str = "") -> dict:
    subtotal = sum(l["total"] for l in lines)
    coupon = find_coupon(coupon_code)
    discount = coupon.discount_for(subtotal) if coupon else 0
    all_digital = bool(lines) and all(l["book"].is_digital for l in lines)
    fee = settings.get_int("shipping_fee", 0)
    free_over = settings.get_int("free_shipping_over", 0)
    shipping = 0 if (not lines or all_digital or (free_over and subtotal - discount >= free_over)) else fee
    return {"subtotal": subtotal, "discount": discount, "shipping": shipping,
            "total": max(0, subtotal - discount + shipping),
            "coupon": coupon if discount else None, "free_over": free_over, "all_digital": all_digital}


# ------------------------------------------------------------------ orders

def _new_code() -> str:
    while True:
        code = "N" + "".join(secrets.choice("23456789") for _ in range(7))
        if not Order.query.filter_by(code=code).first():
            return code


def create_order(lines: list[dict], customer: dict, method: str, coupon_code: str = "") -> Order:
    if not lines:
        raise OrderError("سبد خرید خالی است.")
    t = totals(lines, coupon_code)
    order = Order(code=_new_code(), payment_method=method, subtotal=t["subtotal"], discount=t["discount"],
                  shipping=t["shipping"], total=t["total"],
                  coupon_code=t["coupon"].code if t["coupon"] else "", **customer)
    for line in lines:
        # Lock-free stock check + decrement in the same transaction.
        b = db.session.get(Book, line["book"].id)
        if b is None or not b.is_active:
            raise OrderError("یکی از کتاب‌های سبد دیگر موجود نیست.")
        if not b.is_digital:
            if (b.stock or 0) < line["qty"]:
                raise OrderError(f"موجودی «{b.title}» کافی نیست.")
            b.stock -= line["qty"]
        img = b.cover.path_sm if b.cover else ""
        order.items.append(OrderItem(book_id=b.id, title=b.title, price=b.price, qty=line["qty"], image=img))
    if t["coupon"]:
        t["coupon"].used = (t["coupon"].used or 0) + 1
    order.payments.append(Payment(method=method, amount=order.total, status="pending"))
    db.session.add(order)
    db.session.commit()
    return order


def _restore_stock(order: Order) -> None:
    for item in order.items:
        if item.book and not item.book.is_digital:
            item.book.stock = (item.book.stock or 0) + item.qty


def set_status(order: Order, new_status: str) -> None:
    """Change status and keep stock consistent when an order is cancelled or revived."""
    old = order.status
    if old == new_status:
        return
    if old in STOCK_HELD_STATUSES and new_status == "cancelled":
        _restore_stock(order)
    elif old == "cancelled" and new_status in STOCK_HELD_STATUSES:
        for item in order.items:
            b = item.book
            if b and not b.is_digital:
                if (b.stock or 0) < item.qty:
                    raise OrderError(f"موجودی «{b.title}» برای بازگرداندن سفارش کافی نیست.")
                b.stock -= item.qty
    order.status = new_status


def submit_receipt(order: Order, payment: Payment, *, last4: str, ref: str, paid_at: str, receipt_path: str) -> None:
    payment.payer_card_last4 = last4
    payment.ref_number = ref
    payment.paid_at_text = paid_at
    payment.receipt_path = receipt_path
    payment.status = "submitted"
    payment.reject_reason = ""
    order.status = "payment_review"
    db.session.commit()


def approve_payment(payment: Payment, admin: str) -> None:
    payment.status = "approved"
    payment.reviewed_by = admin
    payment.reviewed_at = utcnow()
    if payment.order.status in {"awaiting_payment", "payment_review", "payment_rejected", "cancelled"}:
        set_status(payment.order, "paid")
    db.session.commit()


def reject_payment(payment: Payment, admin: str, reason: str) -> None:
    payment.status = "rejected"
    payment.reject_reason = reason[:300]
    payment.reviewed_by = admin
    payment.reviewed_at = utcnow()
    payment.order.status = "payment_rejected"
    # A fresh pending payment lets the customer upload a corrected receipt.
    payment.order.payments.append(Payment(method=payment.method, amount=payment.order.total, status="pending"))
    db.session.commit()


def expire_unpaid(hours: int) -> int:
    """Cancel card-to-card orders that never got a receipt, releasing their stock."""
    cutoff = utcnow() - timedelta(hours=hours)
    stale = Order.query.filter(Order.status == "awaiting_payment", Order.created_at < cutoff).all()
    for order in stale:
        set_status(order, "cancelled")
        order.admin_note = ((order.admin_note or "") + "\nلغو خودکار: رسید پرداخت در مهلت ثبت نشد.").strip()
    db.session.commit()
    return len(stale)
