"""Site settings stored in the database and edited from the admin panel."""
from __future__ import annotations

from flask import g

from .extensions import db
from .models import Setting

DEFAULTS: dict[str, str] = {
    # General / identity
    "site_name": "Nila Bostanpira",
    "site_name_fa": "نیلا بستان‌پیرا",
    "author_name": "نیلا بستان‌پیرا",
    "tagline": "شعر، داستان و کلماتی که می‌مانند",
    "hero_title": "کلمه‌ها، وقتی خانه‌ای پیدا می‌کنند",
    "hero_subtitle": "دنیای شعر و داستان نیلا بستان‌پیرا؛ نوشته‌هایی برای خواندن، و کتاب‌هایی برای داشتن.",
    "about_title": "درباره‌ی نیلا",
    "about_text": (
        "نیلا بستان‌پیرا شاعر و نویسنده‌ای است که کلمه‌ها را دانه‌به‌دانه، مثل یک باغبان صبور، می‌چیند. "
        "این بخش را از پنل مدیریت با زندگی‌نامه‌ی واقعی خودتان جایگزین کنید."
    ),
    "phone": "",
    "email": "",
    "instagram": "",
    "telegram": "",
    "address": "",
    # Shipping (only used for physical books)
    "shipping_fee": "60000",
    "free_shipping_over": "1500000",
    "processing_days": "۲ تا ۵ روز کاری",
    # Card to card
    "card_number": "",
    "card_holder": "",
    "card_bank": "",
    "sheba": "",
    "card_instructions": "مبلغ سفارش را دقیقاً کارت‌به‌کارت کنید، سپس تصویر رسید و شماره پیگیری را همین‌جا ثبت کنید. پس از تایید، نسخه‌ی دیجیتال فوراً فعال می‌شود یا کتاب چاپی ارسال می‌شود.",
    "card_to_card_enabled": "1",
    # Online gateway (ZarinPal) — off until the admin sets a merchant id and tests it
    "gateway_enabled": "0",
}


def get_all() -> dict[str, str]:
    if "settings" not in g:
        values = dict(DEFAULTS)
        for row in Setting.query.all():
            values[row.key] = row.value or ""
        g.settings = values
    return g.settings


def get(key: str, default: str = "") -> str:
    return get_all().get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key, str(default)) or default)
    except ValueError:
        return default


def save(values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            db.session.add(row)
        row.value = (value or "").strip()
    db.session.commit()
    g.pop("settings", None)
