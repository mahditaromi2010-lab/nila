"""Admin panel: dashboard, books, categories, poems, banners, pages, orders & payments,
coupons, messages, settings, account & security."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, url_for)
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

from .. import orders, settings
from ..extensions import db, limiter
from ..media import ImageError, delete_media, media_root, save_banner_image, save_book_cover, save_book_pdf
from ..models import (BANNER_PLACEMENTS, ORDER_STATUSES, POEM_PLACEMENTS, AdminUser, AuditLog, Banner, Book,
                      BookImage, Category, Coupon, Message, Order, OrderItem, Page, Payment, Poem)
from ..settings import DEFAULTS
from ..utils import en_digits, parse_int, slugify, utcnow, valid_card
from .security import (admin_required, audit, current_admin, end_session, is_locked, new_totp_secret,
                       provisioning_uri, register_failure, start_session, verify_totp)

admin_bp = Blueprint("admin", __name__, template_folder="../templates/admin")
_DUMMY_HASH = generate_password_hash("not-a-real-password")


@admin_bp.context_processor
def nav_counts():
    if current_admin() is None:
        return {}
    return {"nav_review": Order.query.filter_by(status="payment_review").count(),
            "nav_msgs": Message.query.filter_by(is_read=False).count()}


@admin_bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ------------------------------------------------------------------ auth

@admin_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["ADMIN_LOGIN_RATE_LIMIT"], methods=["POST"])
def login():
    if current_admin():
        return redirect(url_for("admin.dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        code = request.form.get("code") or ""
        user = AdminUser.query.filter(func.lower(AdminUser.username) == username.lower()).first() if username else None
        if user is None:
            check_password_hash(_DUMMY_HASH, password)  # same timing as a real check
            error = "نام کاربری، رمز یا کد اشتباه است."
        elif not user.is_active or is_locked(user):
            error = "این حساب موقتاً قفل است. چند دقیقه دیگر دوباره امتحان کن."
        elif not user.check_password(password) or (user.totp_secret and not verify_totp(user, code)):
            register_failure(user)
            audit("login_failed", user.username, admin=user.username)
            error = "نام کاربری، رمز یا کد اشتباه است."
        else:
            start_session(user)
            audit("login", user.username)
            nxt = request.args.get("next") or ""
            prefix = current_app.config["ADMIN_PATH"]
            return redirect(nxt if nxt.startswith(prefix + "/") and "//" not in nxt else url_for("admin.dashboard"))
    return render_template("admin/login.html", error=error)


@admin_bp.route("/logout", methods=["POST"])
def logout():
    if current_admin():
        audit("logout")
    end_session()
    return redirect(url_for("admin.login"))


# ------------------------------------------------------------------ dashboard

@admin_bp.route("/")
@admin_required
def dashboard():
    today = utcnow() - timedelta(days=1)
    month = utcnow() - timedelta(days=30)
    paid_states = ["paid", "shipped", "delivered"]
    stats = {
        "review": Order.query.filter_by(status="payment_review").count(),
        "awaiting": Order.query.filter_by(status="awaiting_payment").count(),
        "to_ship": Order.query.filter_by(status="paid").count(),
        "orders_24h": Order.query.filter(Order.created_at >= today).count(),
        "revenue_30d": db.session.query(func.coalesce(func.sum(Order.total), 0))
        .filter(Order.status.in_(paid_states), Order.created_at >= month).scalar(),
        "revenue_all": db.session.query(func.coalesce(func.sum(Order.total), 0))
        .filter(Order.status.in_(paid_states)).scalar(),
        "books": Book.query.filter_by(is_active=True).count(),
        "poems": Poem.query.filter_by(is_active=True).count(),
        "messages": Message.query.filter_by(is_read=False).count(),
    }
    low_stock = (Book.query.filter(Book.is_active.is_(True), Book.format == "physical", Book.stock <= 2)
                 .order_by(Book.stock).limit(8).all())
    recent = Order.query.order_by(Order.id.desc()).limit(8).all()
    days = []
    for i in range(13, -1, -1):
        start = (utcnow() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        total = db.session.query(func.coalesce(func.sum(Order.total), 0)).filter(
            Order.status.in_(paid_states), Order.created_at >= start, Order.created_at < end).scalar()
        days.append({"date": start, "total": int(total or 0)})
    peak = max([d["total"] for d in days] + [1])
    setup_missing = not settings.get("card_number")
    return render_template("admin/dashboard.html", stats=stats, low_stock=low_stock, recent=recent,
                           days=days, peak=peak, setup_missing=setup_missing)


# ------------------------------------------------------------------ books

BOOK_TEXT_FIELDS = ("title", "subtitle", "author", "short", "description", "excerpt", "full_text",
                    "published_year", "isbn", "sku", "tags")


@admin_bp.route("/books")
@admin_required
def books():
    q = (request.args.get("q") or "").strip()
    cat = parse_int(request.args.get("cat"))
    state = request.args.get("state", "")
    query = Book.query
    if q:
        query = query.filter(or_(Book.title.ilike(f"%{q}%"), Book.sku.ilike(f"%{q}%"), Book.slug.ilike(f"%{q}%")))
    if cat:
        query = query.filter_by(category_id=cat)
    if state == "hidden":
        query = query.filter_by(is_active=False)
    elif state == "out":
        query = query.filter(Book.format == "physical", Book.stock <= 0)
    elif state == "featured":
        query = query.filter_by(is_featured=True)
    pager = query.order_by(Book.id.desc()).paginate(page=parse_int(request.args.get("page"), 1, minimum=1),
                                                     per_page=30, error_out=False)
    return render_template("admin/books.html", pager=pager, categories=Category.query.order_by(Category.sort).all(),
                           q=q, cat=cat, state=state)


def _unique_slug(model, base: str, item_id: int | None = None) -> str:
    slug, n = base, 2
    while True:
        other = model.query.filter_by(slug=slug).first()
        if other is None or other.id == item_id:
            return slug
        slug = f"{base}-{n}"
        n += 1


def _fill_book(b: Book) -> list[str]:
    f = request.form
    errors = []
    for name in BOOK_TEXT_FIELDS:
        setattr(b, name, (f.get(name) or "").strip())
    if len(b.title) < 2:
        errors.append("عنوان کتاب لازم است.")
    b.slug = _unique_slug(Book, slugify(f.get("slug") or b.title), b.id)
    b.category_id = parse_int(f.get("category_id")) or None
    b.format = f.get("format") if f.get("format") in {"pdf", "physical"} else "pdf"
    b.price = parse_int(f.get("price"), 0, minimum=0)
    b.compare_price = parse_int(f.get("compare_price"), 0, minimum=0)
    b.stock = parse_int(f.get("stock"), 0, minimum=0)
    b.pages = parse_int(f.get("pages"), 0, minimum=0)
    b.sort = parse_int(f.get("sort"), 0)
    b.is_free_read = f.get("is_free_read") == "1"
    b.is_featured = f.get("is_featured") == "1"
    b.is_active = f.get("is_active") == "1"
    return errors


@admin_bp.route("/books/new", methods=["GET", "POST"])
@admin_required
def book_new():
    b = Book(is_active=True, format="pdf", stock=0)
    if request.method == "POST":
        errors = _fill_book(b)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db.session.add(b)
            db.session.flush()
            _save_book_uploads(b)
            db.session.commit()
            audit("book_create", b.slug, b.title)
            flash("کتاب ساخته شد.", "success")
            return redirect(url_for("admin.book_edit", bid=b.id))
    return render_template("admin/book_form.html", b=b, categories=Category.query.order_by(Category.sort).all())


def _save_book_uploads(b: Book) -> int:
    count = 0
    start = max([i.sort for i in b.images] + [0]) + 1
    for i, file in enumerate(request.files.getlist("images")):
        if not file or not file.filename:
            continue
        try:
            big, small = save_book_cover(file)
        except ImageError as exc:
            flash(f"{file.filename}: {exc}", "error")
            continue
        b.images.append(BookImage(path=big, path_sm=small, alt=b.title, sort=start + i))
        count += 1
    pdf_file = request.files.get("pdf_file")
    if pdf_file and pdf_file.filename:
        try:
            if b.pdf_path:
                delete_media(b.pdf_path)
            b.pdf_path = save_book_pdf(pdf_file)
        except ImageError as exc:
            flash(str(exc), "error")
    return count


@admin_bp.route("/books/<int:bid>", methods=["GET", "POST"])
@admin_required
def book_edit(bid):
    b = db.session.get(Book, bid) or abort(404)
    if request.method == "POST":
        errors = _fill_book(b)
        for img in b.images:
            alt = request.form.get(f"alt_{img.id}")
            if alt is not None:
                img.alt = alt.strip()[:160]
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
        else:
            added = _save_book_uploads(b)
            db.session.commit()
            audit("book_update", b.slug, f"+{added} images" if added else "")
            flash("تغییرات ذخیره شد.", "success")
            return redirect(url_for("admin.book_edit", bid=b.id))
    sold = db.session.query(func.coalesce(func.sum(OrderItem.qty), 0)).join(Order).filter(
        OrderItem.book_id == b.id, Order.status.in_(["paid", "shipped", "delivered"])).scalar()
    return render_template("admin/book_form.html", b=b, categories=Category.query.order_by(Category.sort).all(),
                           sold=sold)


@admin_bp.route("/books/<int:bid>/image/<int:iid>", methods=["POST"])
@admin_required
def book_image(bid, iid):
    b = db.session.get(Book, bid) or abort(404)
    img = next((i for i in b.images if i.id == iid), None) or abort(404)
    action = request.form.get("action")
    ordered = list(b.images)
    idx = ordered.index(img)
    if action == "delete":
        delete_media(img.path, img.path_sm)
        b.images.remove(img)
    elif action == "main":
        ordered.insert(0, ordered.pop(idx))
    elif action == "up" and idx > 0:
        ordered[idx - 1], ordered[idx] = ordered[idx], ordered[idx - 1]
    elif action == "down" and idx < len(ordered) - 1:
        ordered[idx + 1], ordered[idx] = ordered[idx], ordered[idx + 1]
    if action != "delete":
        for n, item in enumerate(ordered):
            item.sort = n
    db.session.commit()
    audit("book_image_" + (action or ""), b.slug)
    return redirect(url_for("admin.book_edit", bid=b.id) + "#images")


@admin_bp.route("/books/<int:bid>/action", methods=["POST"])
@admin_required
def book_action(bid):
    b = db.session.get(Book, bid) or abort(404)
    action = request.form.get("action")
    if action == "toggle":
        b.is_active = not b.is_active
    elif action == "feature":
        b.is_featured = not b.is_featured
    elif action == "delete":
        if request.form.get("confirm") != b.slug:
            flash("برای حذف، نامک (slug) کتاب را دقیق تایپ کن.", "error")
            return redirect(url_for("admin.book_edit", bid=b.id))
        for img in b.images:
            delete_media(img.path, img.path_sm)
        if b.pdf_path:
            delete_media(b.pdf_path)
        OrderItem.query.filter_by(book_id=b.id).update({"book_id": None})
        db.session.delete(b)
        db.session.commit()
        audit("book_delete", b.slug, b.title)
        flash("کتاب حذف شد.", "success")
        return redirect(url_for("admin.books"))
    db.session.commit()
    audit("book_" + (action or ""), b.slug)
    return redirect(request.referrer or url_for("admin.books"))


# ------------------------------------------------------------------ categories

@admin_bp.route("/categories", methods=["GET", "POST"])
@admin_required
def categories():
    if request.method == "POST":
        cid = parse_int(request.form.get("id"))
        c = db.session.get(Category, cid) if cid else Category()
        if c is None:
            abort(404)
        if request.form.get("action") == "delete" and cid:
            if c.books.count():
                flash("این دسته کتاب دارد؛ اول کتاب‌ها را جابه‌جا کن.", "error")
            else:
                db.session.delete(c)
                db.session.commit()
                audit("category_delete", c.slug)
                flash("دسته حذف شد.", "success")
            return redirect(url_for("admin.categories"))
        c.name = (request.form.get("name") or "").strip()[:120]
        if not c.name:
            flash("نام دسته لازم است.", "error")
            return redirect(url_for("admin.categories"))
        slug = slugify(request.form.get("slug") or c.name)
        clash = Category.query.filter_by(slug=slug).first()
        if clash and clash.id != c.id:
            slug = f"{slug}-{Category.query.count() + 1}"
        c.slug = slug
        c.description = (request.form.get("description") or "").strip()[:300]
        c.sort = parse_int(request.form.get("sort"))
        c.is_active = request.form.get("is_active") == "1"
        db.session.add(c)
        db.session.commit()
        audit("category_save", c.slug)
        flash("دسته ذخیره شد.", "success")
        return redirect(url_for("admin.categories"))
    rows = Category.query.order_by(Category.sort).all()
    return render_template("admin/categories.html", rows=rows)


# ------------------------------------------------------------------ poems

@admin_bp.route("/poems", methods=["GET"])
@admin_required
def poems():
    rows = Poem.query.order_by(Poem.sort, Poem.id.desc()).all()
    return render_template("admin/poems.html", rows=rows, placements=POEM_PLACEMENTS)


@admin_bp.route("/poems/new", methods=["GET", "POST"])
@admin_required
def poem_new():
    p = Poem(is_active=True, body="")
    return _poem_form(p, creating=True)


@admin_bp.route("/poems/<int:pid>", methods=["GET", "POST"])
@admin_required
def poem_edit(pid):
    p = db.session.get(Poem, pid) or abort(404)
    return _poem_form(p, creating=False)


def _poem_form(p: Poem, creating: bool):
    if request.method == "POST":
        f = request.form
        p.title = (f.get("title") or "").strip()[:160]
        p.body = (f.get("body") or "").strip()
        p.collection = (f.get("collection") or "").strip()[:120]
        p.placements = ",".join(k for k in POEM_PLACEMENTS if f.get(f"place_{k}") == "1") or "poems_page"
        p.is_featured = f.get("is_featured") == "1"
        p.is_active = f.get("is_active") == "1"
        p.sort = parse_int(f.get("sort"), 0)
        if not p.body:
            flash("متن شعر نمی‌تواند خالی باشد.", "error")
            return render_template("admin/poem_form.html", p=p, placements=POEM_PLACEMENTS, creating=creating)
        if creating:
            base = slugify(f.get("title") or p.body[:30])
            p.slug = _unique_slug(Poem, base)
            db.session.add(p)
        db.session.commit()
        audit("poem_save", p.slug)
        flash("شعر ذخیره شد.", "success")
        return redirect(url_for("admin.poems"))
    return render_template("admin/poem_form.html", p=p, placements=POEM_PLACEMENTS, creating=creating)


@admin_bp.route("/poems/<int:pid>/action", methods=["POST"])
@admin_required
def poem_action(pid):
    p = db.session.get(Poem, pid) or abort(404)
    action = request.form.get("action")
    if action == "toggle":
        p.is_active = not p.is_active
    elif action == "feature":
        p.is_featured = not p.is_featured
    elif action == "delete":
        db.session.delete(p)
        db.session.commit()
        audit("poem_delete", p.slug)
        flash("شعر حذف شد.", "success")
        return redirect(url_for("admin.poems"))
    db.session.commit()
    audit("poem_" + (action or ""), p.slug)
    return redirect(url_for("admin.poems"))


# ------------------------------------------------------------------ banners

@admin_bp.route("/banners", methods=["GET"])
@admin_required
def banners():
    rows = Banner.query.order_by(Banner.placement, Banner.sort).all()
    return render_template("admin/banners.html", rows=rows, placements=BANNER_PLACEMENTS)


@admin_bp.route("/banners/new", methods=["GET", "POST"])
@admin_required
def banner_new():
    b = Banner(is_active=True)
    return _banner_form(b)


@admin_bp.route("/banners/<int:bid>", methods=["GET", "POST"])
@admin_required
def banner_edit(bid):
    b = db.session.get(Banner, bid) or abort(404)
    return _banner_form(b)


def _banner_form(b: Banner):
    if request.method == "POST":
        f = request.form
        b.title = (f.get("title") or "").strip()[:200]
        b.subtitle = (f.get("subtitle") or "").strip()[:300]
        b.quote_text = (f.get("quote_text") or "").strip()
        b.link_url = (f.get("link_url") or "").strip()[:300]
        b.link_label = (f.get("link_label") or "").strip()[:80]
        b.placement = f.get("placement") if f.get("placement") in BANNER_PLACEMENTS else "home_middle"
        b.style = f.get("style") if f.get("style") in {"ink", "gold", "paper"} else "ink"
        b.sort = parse_int(f.get("sort"), 0)
        b.is_active = f.get("is_active") == "1"
        file = request.files.get("image")
        if file and file.filename:
            try:
                big, small = save_banner_image(file)
                if b.image:
                    delete_media(b.image, b.image_sm)
                b.image, b.image_sm = big, small
            except ImageError as exc:
                flash(str(exc), "error")
        db.session.add(b)
        db.session.commit()
        audit("banner_save", str(b.id))
        flash("بنر ذخیره شد.", "success")
        return redirect(url_for("admin.banners"))
    return render_template("admin/banner_form.html", b=b, placements=BANNER_PLACEMENTS)


@admin_bp.route("/banners/<int:bid>/action", methods=["POST"])
@admin_required
def banner_action(bid):
    b = db.session.get(Banner, bid) or abort(404)
    action = request.form.get("action")
    if action == "toggle":
        b.is_active = not b.is_active
    elif action == "delete":
        if b.image:
            delete_media(b.image, b.image_sm)
        db.session.delete(b)
        db.session.commit()
        audit("banner_delete", str(bid))
        flash("بنر حذف شد.", "success")
        return redirect(url_for("admin.banners"))
    db.session.commit()
    return redirect(url_for("admin.banners"))


# ------------------------------------------------------------------ pages

@admin_bp.route("/pages", methods=["GET"])
@admin_required
def pages():
    return render_template("admin/pages.html", rows=Page.query.order_by(Page.slug).all())


@admin_bp.route("/pages/new", methods=["GET", "POST"])
@admin_required
def page_new():
    p = Page()
    return _page_form(p, creating=True)


@admin_bp.route("/pages/<int:pid>", methods=["GET", "POST"])
@admin_required
def page_edit(pid):
    p = db.session.get(Page, pid) or abort(404)
    return _page_form(p, creating=False)


def _page_form(p: Page, creating: bool):
    if request.method == "POST":
        f = request.form
        p.title = (f.get("title") or "").strip()[:160]
        p.body = (f.get("body") or "").strip()
        if creating:
            p.slug = _unique_slug(Page, slugify(f.get("slug") or p.title))
            db.session.add(p)
        if not p.title:
            flash("عنوان صفحه لازم است.", "error")
            return render_template("admin/page_form.html", p=p, creating=creating)
        db.session.commit()
        audit("page_save", p.slug)
        flash("صفحه ذخیره شد.", "success")
        return redirect(url_for("admin.pages"))
    return render_template("admin/page_form.html", p=p, creating=creating)


@admin_bp.route("/pages/<int:pid>/delete", methods=["POST"])
@admin_required
def page_delete(pid):
    p = db.session.get(Page, pid) or abort(404)
    db.session.delete(p)
    db.session.commit()
    audit("page_delete", p.slug)
    flash("صفحه حذف شد.", "success")
    return redirect(url_for("admin.pages"))


# ------------------------------------------------------------------ orders

@admin_bp.route("/orders")
@admin_required
def orders_list():
    status = request.args.get("status", "")
    q = en_digits((request.args.get("q") or "").strip())
    query = Order.query
    if status in ORDER_STATUSES:
        query = query.filter_by(status=status)
    if q:
        query = query.filter(or_(Order.code.ilike(f"%{q}%"), Order.phone.ilike(f"%{q}%"),
                                 Order.customer_name.ilike(f"%{q}%")))
    pager = query.order_by(Order.id.desc()).paginate(page=parse_int(request.args.get("page"), 1, minimum=1),
                                                     per_page=30, error_out=False)
    counts = dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    return render_template("admin/orders.html", pager=pager, status=status, q=q, counts=counts,
                           statuses=ORDER_STATUSES)


@admin_bp.route("/orders/export.csv")
@admin_required
def orders_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "date", "status", "name", "phone", "province", "city", "address", "postal_code",
                "subtotal", "shipping", "discount", "total", "items"])
    for o in Order.query.order_by(Order.id).all():
        w.writerow([o.code, o.created_at.isoformat(timespec="minutes"), o.status, o.customer_name, o.phone,
                    o.province, o.city, o.address, o.postal_code, o.subtotal, o.shipping, o.discount, o.total,
                    " | ".join(f"{i.title} x{i.qty}" for i in o.items)])
    audit("orders_export")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})


@admin_bp.route("/orders/<code>", methods=["GET", "POST"])
@admin_required
def order_detail(code):
    order = Order.query.filter_by(code=code).first_or_404()
    admin = current_admin().username
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "approve":
                payment = db.session.get(Payment, parse_int(request.form.get("payment_id"))) or abort(404)
                if payment.order_id != order.id:
                    abort(400)
                orders.approve_payment(payment, admin)
                audit("payment_approve", order.code, f"payment {payment.id}")
                flash("پرداخت تایید شد.", "success")
            elif action == "reject":
                payment = db.session.get(Payment, parse_int(request.form.get("payment_id"))) or abort(404)
                if payment.order_id != order.id:
                    abort(400)
                reason = (request.form.get("reason") or "").strip() or "رسید با مبلغ یا مشخصات سفارش مطابقت نداشت."
                orders.reject_payment(payment, admin, reason)
                audit("payment_reject", order.code, reason)
                flash("پرداخت رد شد؛ مشتری می‌تواند رسید درست را دوباره ثبت کند.", "success")
            elif action == "status":
                new = request.form.get("status")
                if new not in ORDER_STATUSES:
                    abort(400)
                old = order.status
                orders.set_status(order, new)
                tracking = (request.form.get("post_tracking") or "").strip()[:60]
                if tracking:
                    order.post_tracking = en_digits(tracking)
                db.session.commit()
                audit("order_status", order.code, f"{old} -> {new}")
                flash("وضعیت سفارش به‌روز شد.", "success")
            elif action == "note":
                order.admin_note = (request.form.get("admin_note") or "").strip()[:3000]
                db.session.commit()
                audit("order_note", order.code)
                flash("یادداشت ذخیره شد.", "success")
        except orders.OrderError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("admin.order_detail", code=order.code))
    return render_template("admin/order_detail.html", order=order, statuses=ORDER_STATUSES)


@admin_bp.route("/receipts/<path:name>")
@admin_required
def receipt(name):
    if not Payment.query.filter_by(receipt_path=f"receipts/{name}").first():
        abort(404)
    return send_from_directory(media_root() / "receipts", name)


@admin_bp.route("/orders/expire", methods=["POST"])
@admin_required
def orders_expire():
    n = orders.expire_unpaid(current_app.config["ORDER_HOLD_HOURS"])
    audit("orders_expire", "", str(n))
    flash(f"{n} سفارش پرداخت‌نشده لغو شد.", "success")
    return redirect(url_for("admin.orders_list"))


# ------------------------------------------------------------------ coupons

@admin_bp.route("/coupons", methods=["GET", "POST"])
@admin_required
def coupons():
    if request.method == "POST":
        cid = parse_int(request.form.get("id"))
        c = db.session.get(Coupon, cid) if cid else Coupon()
        if c is None:
            abort(404)
        if request.form.get("action") == "delete" and cid:
            db.session.delete(c)
            db.session.commit()
            audit("coupon_delete", c.code)
            return redirect(url_for("admin.coupons"))
        code = en_digits((request.form.get("code") or "").strip().upper())[:32]
        if not code or (Coupon.query.filter_by(code=code).first() not in (None, c)):
            flash("کد خالی یا تکراری است.", "error")
            return redirect(url_for("admin.coupons"))
        c.code = code
        c.percent = parse_int(request.form.get("percent"), 0, minimum=0, maximum=100)
        c.amount = parse_int(request.form.get("amount"), 0, minimum=0)
        c.min_total = parse_int(request.form.get("min_total"), 0, minimum=0)
        c.max_uses = parse_int(request.form.get("max_uses"), 0, minimum=0)
        days = parse_int(request.form.get("days"), 0, minimum=0)
        if days:
            c.expires_at = utcnow() + timedelta(days=days)
        c.is_active = request.form.get("is_active") == "1"
        db.session.add(c)
        db.session.commit()
        audit("coupon_save", c.code)
        flash("کد تخفیف ذخیره شد.", "success")
        return redirect(url_for("admin.coupons"))
    return render_template("admin/coupons.html", rows=Coupon.query.order_by(Coupon.id.desc()).all())


# ------------------------------------------------------------------ messages

@admin_bp.route("/messages", methods=["GET", "POST"])
@admin_required
def messages():
    if request.method == "POST":
        m = db.session.get(Message, parse_int(request.form.get("id"))) or abort(404)
        if request.form.get("action") == "delete":
            db.session.delete(m)
        else:
            m.is_read = not m.is_read
        db.session.commit()
        return redirect(url_for("admin.messages"))
    return render_template("admin/messages.html", rows=Message.query.order_by(Message.id.desc()).limit(300).all())


# ------------------------------------------------------------------ settings

SETTING_GROUPS = {
    "general": ["site_name", "site_name_fa", "author_name", "tagline", "hero_title", "hero_subtitle", "about_title",
                "about_text", "phone", "email", "instagram", "telegram", "address"],
    "payment": ["card_to_card_enabled", "card_number", "card_holder", "card_bank", "sheba", "card_instructions",
                "gateway_enabled"],
    "shipping": ["shipping_fee", "free_shipping_over", "processing_days"],
}
CHECKBOXES = {"card_to_card_enabled", "gateway_enabled"}


@admin_bp.route("/settings/<group>", methods=["GET", "POST"])
@admin_required
def settings_page(group):
    if group not in SETTING_GROUPS:
        abort(404)
    if request.method == "POST":
        values = {}
        for key in SETTING_GROUPS[group]:
            if key in CHECKBOXES:
                values[key] = "1" if request.form.get(key) == "1" else "0"
            else:
                values[key] = request.form.get(key, "")
        if group == "payment":
            values["card_number"] = en_digits(values["card_number"]).replace(" ", "").replace("-", "")
            values["sheba"] = en_digits(values["sheba"]).replace(" ", "").upper()
            if values["card_number"] and not valid_card(values["card_number"]):
                flash("شماره کارت معتبر نیست (۱۶ رقم را دوباره بررسی کن).", "error")
                return redirect(url_for("admin.settings_page", group=group))
            if values["sheba"] and not (values["sheba"].startswith("IR") and len(values["sheba"]) == 26):
                flash("شبا باید با IR شروع شود و ۲۶ کاراکتر باشد.", "error")
                return redirect(url_for("admin.settings_page", group=group))
        if group == "shipping":
            for key in ("shipping_fee", "free_shipping_over"):
                values[key] = str(parse_int(values[key], 0, minimum=0))
        settings.save(values)
        audit("settings_" + group)
        flash("تنظیمات ذخیره شد.", "success")
        return redirect(url_for("admin.settings_page", group=group))
    gateway_ready = bool(current_app.config.get("ZARINPAL_MERCHANT_ID") and current_app.config.get("PUBLIC_BASE_URL"))
    return render_template("admin/settings.html", group=group, keys=SETTING_GROUPS[group], s=settings.get_all(),
                           defaults=DEFAULTS, gateway_ready=gateway_ready,
                           sandbox=current_app.config.get("ZARINPAL_SANDBOX"))


# ------------------------------------------------------------------ admins & security

@admin_bp.route("/account", methods=["GET", "POST"])
@admin_required
def account():
    me = current_admin()
    pending_secret = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            if not me.check_password(request.form.get("current") or ""):
                flash("رمز فعلی درست نیست.", "error")
            elif len(request.form.get("new") or "") < 10:
                flash("رمز جدید دست‌کم ۱۰ کاراکتر باشد.", "error")
            elif request.form.get("new") != request.form.get("repeat"):
                flash("تکرار رمز جدید یکسان نیست.", "error")
            else:
                me.set_password(request.form["new"])
                db.session.commit()
                start_session(me)
                audit("password_change")
                flash("رمز عوض شد؛ نشست‌های دیگر بسته شدند.", "success")
            return redirect(url_for("admin.account"))
        if action == "totp_start":
            pending_secret = new_totp_secret()
        elif action == "totp_confirm":
            secret = request.form.get("secret") or ""
            if verify_totp(me, request.form.get("code") or "", secret=secret):
                me.totp_secret = secret
                db.session.commit()
                audit("totp_enable")
                flash("ورود دومرحله‌ای فعال شد.", "success")
                return redirect(url_for("admin.account"))
            flash("کد درست نبود؛ دوباره اسکن کن و کد تازه بزن.", "error")
            pending_secret = secret
        elif action == "totp_disable":
            if me.check_password(request.form.get("current") or ""):
                me.totp_secret = ""
                db.session.commit()
                audit("totp_disable")
                flash("ورود دومرحله‌ای خاموش شد.", "success")
            else:
                flash("رمز فعلی درست نیست.", "error")
            return redirect(url_for("admin.account"))
        elif action == "add_admin":
            username = (request.form.get("username") or "").strip()[:64]
            password = request.form.get("password") or ""
            if len(username) < 3 or len(password) < 10:
                flash("نام کاربری دست‌کم ۳ و رمز دست‌کم ۱۰ کاراکتر.", "error")
            elif AdminUser.query.filter(func.lower(AdminUser.username) == username.lower()).first():
                flash("این نام کاربری وجود دارد.", "error")
            else:
                u = AdminUser(username=username)
                u.set_password(password)
                db.session.add(u)
                db.session.commit()
                audit("admin_create", username)
                flash("مدیر جدید ساخته شد.", "success")
            return redirect(url_for("admin.account"))
        elif action == "toggle_admin":
            u = db.session.get(AdminUser, parse_int(request.form.get("id"))) or abort(404)
            if u.id == me.id:
                flash("نمی‌توانی حساب خودت را غیرفعال کنی.", "error")
            else:
                u.is_active = not u.is_active
                u.session_version = (u.session_version or 0) + 1
                db.session.commit()
                audit("admin_toggle", u.username, str(u.is_active))
            return redirect(url_for("admin.account"))
    uri = provisioning_uri(me, pending_secret) if pending_secret else None
    return render_template("admin/account.html", me=me, admins=AdminUser.query.order_by(AdminUser.id).all(),
                           pending_secret=pending_secret, uri=uri)


@admin_bp.route("/audit")
@admin_required
def audit_log():
    rows = AuditLog.query.order_by(AuditLog.id.desc()).limit(400).all()
    return render_template("admin/audit.html", rows=rows)


__all__ = ["admin_bp"]
