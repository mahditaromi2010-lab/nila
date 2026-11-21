"""Public site: home, books (store), poems, about the poet, cart & checkout, order tracking."""
from __future__ import annotations

import hmac
import re

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from sqlalchemy import or_

from . import orders, settings
from .extensions import db, limiter
from .media import ImageError, media_root, save_receipt
from .models import Banner, Book, Category, Message, Order, Page, Payment, Poem
from .payments import available_methods, get_provider
from .payments.zarinpal import GatewayError
from .utils import PROVINCES, en_digits, excerpt, parse_int, reading_minutes, valid_iran_mobile

shop_bp = Blueprint("shop", __name__)
PER_PAGE = 12
SORTS = {
    "new": Book.created_at.desc(),
    "cheap": Book.price.asc(),
    "expensive": Book.price.desc(),
    "popular": Book.views.desc(),
}


def _active_books():
    return Book.query.filter_by(is_active=True)


def _banners_for(place: str) -> list[Banner]:
    return (Banner.query.filter_by(placement=place, is_active=True).order_by(Banner.sort).all())


def _poems_for(place: str, limit: int = 6) -> list[Poem]:
    rows = Poem.query.filter_by(is_active=True).order_by(Poem.sort, Poem.id.desc()).all()
    return [p for p in rows if p.shows_in(place)][:limit]


@shop_bp.route("/")
def home():
    featured = _active_books().filter_by(is_featured=True).order_by(Book.sort, Book.id).limit(6).all()
    newest = _active_books().order_by(Book.created_at.desc(), Book.id.desc()).limit(6).all()
    return render_template(
        "shop/home.html", featured=featured, newest=newest,
        hero_banners=_banners_for("home_top"), mid_banners=_banners_for("home_middle"),
        hero_poems=_poems_for("home_hero", limit=1), feed_poems=_poems_for("home_feed", limit=3))


@shop_bp.route("/books")
def catalog():
    q = (request.args.get("q") or "").strip()[:80]
    cat_slug = request.args.get("cat") or ""
    sort = request.args.get("sort") if request.args.get("sort") in SORTS else "new"
    page = parse_int(request.args.get("page"), 1, minimum=1)

    query = _active_books()
    category = None
    if cat_slug:
        category = Category.query.filter_by(slug=cat_slug, is_active=True).first_or_404()
        query = query.filter(Book.category_id == category.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Book.title.ilike(like), Book.short.ilike(like), Book.tags.ilike(like)))
    pager = query.order_by(SORTS[sort], Book.id.desc()).paginate(page=page, per_page=PER_PAGE, error_out=False)
    categories = Category.query.filter_by(is_active=True).order_by(Category.sort).all()
    return render_template("shop/catalog.html", pager=pager, categories=categories, category=category,
                           q=q, sort=sort, top_banners=_banners_for("shop_top"))


@shop_bp.route("/book/<slug>")
def book(slug):
    b = _active_books().filter_by(slug=slug).first_or_404()
    b.views = (b.views or 0) + 1
    db.session.commit()
    related = (_active_books().filter(Book.category_id == b.category_id, Book.id != b.id)
               .order_by(Book.is_featured.desc(), Book.id.desc()).limit(4).all())
    return render_template("shop/book.html", b=b, related=related)


@shop_bp.route("/book/<slug>/read")
def book_read(slug):
    b = _active_books().filter_by(slug=slug).first_or_404()
    if not (b.is_free_read and b.full_text):
        abort(404)
    return render_template("shop/read.html", b=b, minutes=reading_minutes(b.full_text))


@shop_bp.route("/poems")
def poems():
    rows = [p for p in Poem.query.filter_by(is_active=True).order_by(Poem.sort, Poem.id.desc()).all()
            if p.shows_in("poems_page")]
    return render_template("shop/poems.html", rows=rows, top_banners=_banners_for("about_top"))


@shop_bp.route("/poem/<slug>")
def poem(slug):
    p = Poem.query.filter_by(slug=slug, is_active=True).first_or_404()
    return render_template("shop/poem.html", p=p)


@shop_bp.route("/about")
def about():
    page = Page.query.filter_by(slug="about").first()
    return render_template("shop/about.html", page=page, top_banners=_banners_for("about_top"))


@shop_bp.route("/page/<slug>")
def page_view(slug):
    page = Page.query.filter_by(slug=slug).first_or_404()
    return render_template("shop/page.html", page=page)


@shop_bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def contact():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:120]
        contact_value = (request.form.get("contact") or "").strip()[:160]
        body = (request.form.get("body") or "").strip()[:3000]
        if request.form.get("website"):  # honeypot
            return redirect(url_for("shop.contact"))
        if not name or not contact_value or len(body) < 5:
            flash("لطفاً نام، راه تماس و پیامت را کامل بنویس.", "error")
        else:
            db.session.add(Message(name=name, contact=contact_value, body=body))
            db.session.commit()
            flash("پیامت رسید! به‌زودی جواب می‌دهیم.", "success")
            return redirect(url_for("shop.contact"))
    return render_template("shop/contact.html")


# ------------------------------------------------------------------ cart

@shop_bp.route("/cart")
def cart():
    lines = orders.cart_lines()
    t = orders.totals(lines, session.get("coupon", ""))
    return render_template("shop/cart.html", lines=lines, t=t)


@shop_bp.route("/cart/add", methods=["POST"])
def cart_add():
    b = db.session.get(Book, parse_int(request.form.get("book_id")))
    if b is None or not b.is_active:
        abort(404)
    try:
        orders.cart_add(b, parse_int(request.form.get("qty"), 1, minimum=1, maximum=10))
        flash(f"«{b.title}» به سبد اضافه شد.", "success")
    except orders.OrderError as exc:
        flash(str(exc), "error")
    if request.form.get("next") == "cart":
        return redirect(url_for("shop.cart"))
    return redirect(request.referrer or url_for("shop.book", slug=b.slug))


@shop_bp.route("/cart/update", methods=["POST"])
def cart_update():
    for key, value in request.form.items():
        if key.startswith("qty_"):
            bid = parse_int(key[4:])
            b = db.session.get(Book, bid)
            if b:
                orders.cart_set(bid, min(parse_int(value, 0, minimum=0, maximum=10), b.max_qty()))
    remove = parse_int(request.form.get("remove"))
    if remove:
        orders.cart_set(remove, 0)
    return redirect(url_for("shop.cart"))


@shop_bp.route("/cart/coupon", methods=["POST"])
@limiter.limit("20 per hour")
def cart_coupon():
    code = (request.form.get("code") or "").strip().upper()[:32]
    if not code:
        session.pop("coupon", None)
        return redirect(url_for("shop.cart"))
    lines = orders.cart_lines()
    coupon = orders.find_coupon(code)
    if coupon and coupon.discount_for(sum(l["total"] for l in lines)):
        session["coupon"] = code
        flash("کد تخفیف اعمال شد.", "success")
    else:
        session.pop("coupon", None)
        flash("این کد تخفیف معتبر نیست یا شرایطش برقرار نیست.", "error")
    return redirect(url_for("shop.cart"))


# ------------------------------------------------------------------ checkout

def _remember_order(order: Order) -> None:
    mine = session.get("my_orders") or {}
    mine[order.code] = order.access_token
    session["my_orders"] = dict(list(mine.items())[-10:])


@shop_bp.route("/checkout", methods=["GET", "POST"])
@limiter.limit("15 per hour", methods=["POST"])
def checkout():
    lines = orders.cart_lines()
    if not lines:
        flash("سبد خریدت خالیه.", "error")
        return redirect(url_for("shop.catalog"))
    methods = available_methods()
    t = orders.totals(lines, session.get("coupon", ""))
    form = {k: (request.form.get(k) or "").strip() for k in
            ("customer_name", "phone", "email", "province", "city", "address", "postal_code", "note", "method")}
    errors = {}
    if request.method == "POST":
        form["phone"] = en_digits(form["phone"])
        form["postal_code"] = en_digits(form["postal_code"]).replace("-", "").replace(" ", "")
        if len(form["customer_name"]) < 3:
            errors["customer_name"] = "نام و نام خانوادگی را بنویس."
        if not valid_iran_mobile(form["phone"]):
            errors["phone"] = "شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود."
        if not t["all_digital"]:
            if form["province"] not in PROVINCES:
                errors["province"] = "استان را انتخاب کن."
            if len(form["city"]) < 2:
                errors["city"] = "شهر را بنویس."
            if len(form["address"]) < 10:
                errors["address"] = "نشانی کامل‌تر لازم است."
            if not re.fullmatch(r"\d{10}", form["postal_code"]):
                errors["postal_code"] = "کد پستی ۱۰ رقمی است."
        provider = get_provider(form["method"]) if form["method"] else (methods[0] if len(methods) == 1 else None)
        if provider is None:
            errors["method"] = "روش پرداخت را انتخاب کن."
        if not errors:
            customer = {k: form[k][:500] for k in
                        ("customer_name", "phone", "email", "province", "city", "address", "postal_code", "note")}
            try:
                order = orders.create_order(lines, customer, provider.code, session.get("coupon", ""))
            except orders.OrderError as exc:
                flash(str(exc), "error")
                return redirect(url_for("shop.cart"))
            orders.cart_clear()
            _remember_order(order)
            if provider.online:
                return redirect(url_for("shop.gateway_start", code=order.code, t=order.access_token))
            return redirect(url_for("shop.order_view", code=order.code, t=order.access_token))
    return render_template("shop/checkout.html", lines=lines, t=t, form=form, errors=errors,
                           methods=methods, provinces=PROVINCES)


def _order_or_404(code: str) -> Order:
    order = Order.query.filter_by(code=code).first()
    token = request.values.get("t") or (session.get("my_orders") or {}).get(code, "")
    if order is None or not token or not hmac.compare_digest(order.access_token, token):
        abort(404)
    return order


@shop_bp.route("/order/<code>")
def order_view(code):
    order = _order_or_404(code)
    provider = get_provider("card_to_card")
    account = provider.account() if provider else None
    return render_template("shop/order.html", order=order, account=account, gateway=get_provider("zarinpal"))


@shop_bp.route("/order/<code>/receipt", methods=["POST"])
@limiter.limit("10 per hour")
def order_receipt(code):
    order = _order_or_404(code)
    payment = order.latest_payment
    if order.status not in {"awaiting_payment", "payment_rejected", "payment_review"} or payment is None \
            or payment.status not in {"pending", "submitted"}:
        flash("برای این سفارش امکان ثبت رسید نیست.", "error")
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    last4 = re.sub(r"\D", "", en_digits(request.form.get("last4") or ""))
    ref = en_digits(request.form.get("ref") or "").strip()[:40]
    paid_at = en_digits(request.form.get("paid_at") or "").strip()[:40]
    file = request.files.get("receipt")
    if len(last4) != 4 or len(ref) < 4:
        flash("چهار رقم آخر کارت و شماره پیگیری را درست وارد کن.", "error")
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    receipt_path = payment.receipt_path
    if file and file.filename:
        try:
            receipt_path = save_receipt(file)
        except ImageError as exc:
            flash(str(exc), "error")
            return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    if not receipt_path:
        flash("تصویر رسید را هم پیوست کن.", "error")
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    orders.submit_receipt(order, payment, last4=last4, ref=ref, paid_at=paid_at, receipt_path=receipt_path)
    flash("رسید ثبت شد. پس از بررسی، نسخه‌ی دیجیتال همین‌جا فعال می‌شود.", "success")
    return redirect(url_for("shop.order_view", code=code, t=order.access_token))


@shop_bp.route("/order/<code>/pay")
def gateway_start(code):
    order = _order_or_404(code)
    provider = get_provider("zarinpal")
    if provider is None or order.status not in {"awaiting_payment", "payment_rejected"}:
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    callback = f"{current_app.config['PUBLIC_BASE_URL']}{url_for('shop.gateway_callback', code=code)}"
    try:
        authority, redirect_url = provider.start(order.total, f"سفارش {order.code}", callback, order.phone)
    except GatewayError as exc:
        flash(str(exc) + " می‌توانی کارت‌به‌کارت پرداخت کنی.", "error")
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    db.session.add(Payment(order=order, method="zarinpal", amount=order.total, status="pending",
                           gateway_authority=authority))
    order.payment_method = "zarinpal"
    db.session.commit()
    return redirect(redirect_url)


@shop_bp.route("/payment/zarinpal/<code>")
def gateway_callback(code):
    order = Order.query.filter_by(code=code).first_or_404()
    authority = request.args.get("Authority", "")
    payment = Payment.query.filter_by(order_id=order.id, gateway_authority=authority, method="zarinpal").first()
    provider = get_provider("zarinpal")
    if payment is None or provider is None:
        abort(404)
    if payment.status == "approved":
        return redirect(url_for("shop.order_view", code=code, t=order.access_token))
    if request.args.get("Status") == "OK":
        try:
            payment.gateway_ref_id = provider.verify(payment.amount, authority)
            orders.approve_payment(payment, "zarinpal")
            flash("پرداخت با موفقیت انجام شد.", "success")
        except GatewayError:
            payment.status = "failed"
            db.session.commit()
            flash("پرداخت تایید نشد. اگر مبلغی کم شده، طی ۷۲ ساعت برمی‌گردد.", "error")
    else:
        payment.status = "failed"
        db.session.commit()
        flash("پرداخت لغو شد.", "error")
    return redirect(url_for("shop.order_view", code=code, t=order.access_token))


@shop_bp.route("/order/<code>/download/<int:item_id>")
def order_download(code, item_id):
    """Serve a bought e-book's PDF, only once the order is paid."""
    order = _order_or_404(code)
    if not order.is_paid:
        abort(403)
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None or item.book is None or not item.book.pdf_path:
        abort(404)
    directory = media_root() / "pdfs"
    filename = item.book.pdf_path.split("/", 1)[1]
    response = send_from_directory(directory, filename, as_attachment=True,
                                    download_name=f"{item.book.title}.pdf")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@shop_bp.route("/track", methods=["GET", "POST"])
@limiter.limit("12 per hour", methods=["POST"])
def track():
    if request.method == "POST":
        code = en_digits(request.form.get("code") or "").strip().upper()[:16]
        phone = en_digits(request.form.get("phone") or "").strip()
        order = Order.query.filter_by(code=code).first()
        if order and hmac.compare_digest(order.phone, phone):
            _remember_order(order)
            return redirect(url_for("shop.order_view", code=order.code, t=order.access_token))
        flash("سفارشی با این کد و شماره پیدا نشد.", "error")
    mine = session.get("my_orders") or {}
    recent = Order.query.filter(Order.code.in_(list(mine))).order_by(Order.id.desc()).all() if mine else []
    return render_template("shop/track.html", recent=recent)


# ------------------------------------------------------------------ media, seo

@shop_bp.route("/media/books/<path:name>")
def media(name):
    response = send_from_directory(media_root() / "books", name, max_age=60 * 60 * 24 * 30)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@shop_bp.route("/media/banners/<path:name>")
def banner_media(name):
    response = send_from_directory(media_root() / "banners", name, max_age=60 * 60 * 24 * 30)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@shop_bp.route("/robots.txt")
def robots():
    admin = current_app.config["ADMIN_PATH"]
    body = (f"User-agent: *\nDisallow: {admin}\nDisallow: /cart\nDisallow: /checkout\nDisallow: /order/\n"
            f"Sitemap: {url_for('shop.sitemap', _external=True)}\n")
    return Response(body, mimetype="text/plain")


@shop_bp.route("/sitemap.xml")
def sitemap():
    urls = [url_for("shop.home", _external=True), url_for("shop.catalog", _external=True),
            url_for("shop.poems", _external=True), url_for("shop.about", _external=True),
            url_for("shop.contact", _external=True)]
    urls += [url_for("shop.catalog", cat=c.slug, _external=True) for c in Category.query.filter_by(is_active=True)]
    urls += [url_for("shop.book", slug=b.slug, _external=True) for b in _active_books()]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"<url><loc>{u}</loc></url>" for u in urls]
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


__all__ = ["shop_bp", "settings", "excerpt"]
