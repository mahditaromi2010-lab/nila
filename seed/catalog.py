"""First content for the site: the category shelves, the author's own story as the
flagship book, a handful of atmospheric pull-quotes from that same story used as
poem cards, an about page, and a welcome banner. Everything here is written by
Nila Bostanpira herself (via Mahdi) — nothing here is invented text attributed to her.
Safe to edit or replace entirely from the admin panel afterwards.
"""
from __future__ import annotations

from pathlib import Path

STORY_PATH = Path(__file__).resolve().parent / "story.txt"


def _story_text() -> str:
    return STORY_PATH.read_text(encoding="utf-8") if STORY_PATH.exists() else ""


def seed_all() -> int:
    from app.extensions import db
    from app.models import Banner, Book, Category, Page, Poem
    from app.utils import slugify

    created = 0

    # ---------------- shelves
    novels = Category.query.filter_by(slug="novels").first()
    if novels is None:
        novels = Category(slug="novels", name="رمان و داستان", description="داستان‌های کوتاه و بلند", sort=1)
        db.session.add(novels)
    poetry_cat = Category.query.filter_by(slug="poetry").first()
    if poetry_cat is None:
        poetry_cat = Category(slug="poetry", name="شعر", description="مجموعه‌های شعر", sort=2)
        db.session.add(poetry_cat)
    db.session.flush()

    # ---------------- the flagship book: the author's own story
    story = _story_text()
    paragraphs = [p.strip() for p in story.split("\n\n") if p.strip()]
    excerpt = "\n\n".join(paragraphs[:3])

    if not Book.query.filter_by(slug="dastan-bi-esm").first() and story:
        book = Book(
            slug="dastan-bi-esm",
            title="داستان بی‌اسم",
            subtitle="یک داستان کوتاه از نیلا بستان‌پیرا",
            author="نیلا بستان‌پیرا",
            category=novels,
            format="pdf",
            price=0,
            is_free_read=True,
            is_featured=True,
            is_active=True,
            pages=0,
            published_year="",
            short="داستانی درباره‌ی عشق‌ ناگفته، فقدان، و پناه‌بردن به یک جزیره‌ی دورافتاده.",
            description=excerpt,
            excerpt=excerpt,
            full_text=story,
            sort=0,
        )
        db.session.add(book)
        created += 1

    # ---------------- a few atmospheric lines from her own story, as poem cards
    quotes = [
        ("خورشید در حال غروب کردن بود قرمزی خاصی اطراف خورشید احاطه کرده بود.", "home_hero,poems_page"),
        ("ماه دایره‌ای زرد رنگ از انتهای دریا دیده می‌شد؛ دریا آرام بود و ماسه‌های کنار ساحل زیر نور ماه می‌درخشید.",
         "home_feed,poems_page"),
        ("خورشید کم‌کم از انتهای دریا بالا می‌آمد و انعکاس نورش بر سطح دریا می‌درخشید.", "home_feed,poems_page"),
    ]
    for i, (text, placements) in enumerate(quotes):
        slug = slugify(text[:30])
        if not Poem.query.filter_by(slug=slug).first():
            db.session.add(Poem(slug=slug, title="", body=text, collection="داستان بی‌اسم",
                                placements=placements, is_active=True, is_featured=(i == 0), sort=i))
            created += 1

    # ---------------- about page (placeholder — replace with the real biography)
    if not Page.query.filter_by(slug="about").first():
        db.session.add(Page(
            slug="about", title="درباره‌ی نیلا",
            body=("نیلا بستان‌پیرا شاعر و نویسنده‌ای است که کلمه‌ها را دانه‌به‌دانه می‌چیند.\n\n"
                  "این متن یک نمونه است — از پنل مدیریت، بخش «صفحات»، آن را با زندگی‌نامه‌ی واقعی، "
                  "دغدغه‌ها و دستاوردهای خودتان جای‌گزین کنید.")))
        created += 1

    # ---------------- a welcoming banner at the top of the home page
    if not Banner.query.filter_by(placement="home_top").first():
        db.session.add(Banner(
            title="داستان بی‌اسم", quote_text="اولین داستان نیلا، همین امروز، رایگان و آنلاین.",
            link_label="خواندن داستان", link_url="/book/dastan-bi-esm/read",
            placement="home_top", style="gold", is_active=True, sort=0))
        created += 1

    db.session.commit()
    return created
