"""Unit tests of the GoGreen distinct-product cap and the product-page size check (2026-11-25).

Mafer's decision: identical variants (same brand, same size, same price; e.g. five scents of one
GoGreen 3 L floor cleaner) count as ONE competitor product. On 2026-11-25 the 5-product cap per
search kept the first 5 relevant products in catalog order, so five scents of the same floor
cleaner at 45 SAR filled it. The cap now counts distinct products and goes on through the
relevant list. A product page whose own name states another size or a carton than the catalog
title is dropped (a dropped row is better than a wrong volume).

No browser and no network: the catalog and product pages are small hand-written HTML served by a
fake ``fetch_html`` (URL -> HTML; unknown URLs are a 404), as in ``test_browser_scrapers.py``. The
relevance AI is a fake batch agent. Titles are the real Arabic catalog titles of the 2026-11-25 run.
"""
from __future__ import annotations

import contextlib

import pytest

from tests.conftest import load_module

pytestmark = pytest.mark.unit

load_module("scraper/main.py")  # puts scraper/ on sys.path, like the other scraper tests
from scrapers import gogreen_scraper as gg  # noqa: E402
from services import browser as br  # noqa: E402

BASE = "https://gogreen.com.sa"
CATALOG = BASE + "/products?page={}"


class FakeBrowser:
    @contextlib.contextmanager
    def page(self):
        yield object()


class AllRelevant:
    """Batch relevance: every title is relevant, except those containing a word of ``reject``."""

    def __init__(self, *reject):
        self.reject = reject
        self.calls = []

    def filter_relevant(self, titles, query):
        self.calls.append((list(titles), query))
        return [not any(w in t for w in self.reject) for t in titles]


def _catalog(items):
    boxes = "".join(f'<div class="product-box"><a class="tp-image-product" href="{href}">'
                    f'<img alt="{title}"/></a></div>' for href, title, _ in items)
    return f'<html lang="ar" dir="rtl"><body>{boxes}</body></html>'


def _product(price, name="x"):
    return (f'<html lang="ar"><h1 class="title-product">{name}</h1>'
            f'<p class="product-formatted-price">{price} <svg/></p></html>')


def _site(monkeypatch, items, pages=None):
    """``items``: [(href, catalog title, price)]; ``pages`` overrides a product page by href."""
    pages = pages or {}
    served = {CATALOG.format(1): _catalog(items)}
    for href, _, price in items:
        served[BASE + href] = pages.get(href, _product(price))
    visited = []

    def fetch(page, url, wait_for=None, timeout_ms=None):
        visited.append(url)
        value = served.get(url)
        if value is None:
            raise br.HttpStatusError(404, url)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gg, "fetch_html", fetch)
    return visited


FLOOR_SCENTS = [
    ("/products/f1", "منظف الارضيات 3 لتر - ليمون", "45"),
    ("/products/f2", "منظف الارضيات 3 لتر - سويت كلفر", "45"),
    ("/products/f3", "منظف الارضيات 3 لتر - توت بري", "45"),
    ("/products/f4", "منظف الارضيات 3 لتر - صنوبر", "45"),
    ("/products/f5", "منظف الارضيات 3 لتر - زهرة الوادى", "45"),
]
OTHER_FLOOR = [
    ("/products/g1", "منظف و مطهر الاسطح بدون عطر ولون 5 لتر", "58"),
    ("/products/g2", "منظف و مطهر الاسطح بدون عطر ولون 30 لتر", "245"),
    ("/products/g3", "ملمع الزجاج 650 مل", "15"),
    ("/products/g4", "منظف أفران المطابخ GOGREEN - 1 لتر", "16"),
    ("/products/g5", "منظف وملمع المعادن والستانلس ستيل - 1 لتر", "23"),
]


# --- variant_key ----------------------------------------------------------------------------------
@pytest.mark.parametrize("a, b", [
    ("منظف الارضيات 3 لتر - ليمون", "منظف الارضيات 3 لتر - زهرة الوادى"),  # trailing scent
    ("صابون ايدي مضاد للبكتيريا 350 مل - ورد", "صابون ايدي مضاد للبكتيريا 350 مل -مسك"),
    ("صابون سائل غسيل الصحون بعطر الليمون 1 لتر", "صابون سائل غسيل الصحون بعطر زهرة الوادي 1 لتر"),
    ("منعم و معطر الملابس بعطر اللافندر 2 لتر", "منعم و معطر الملابس بعطر عبير الزهور 2 لتر"),
    ("صابون الايدي برائحة العود 1 لتر", "صابون الايدي برائحة الورد 1 لتر"),
    ("Floor Cleaner 3 L - Lemon", "floor cleaner 3 l - Pine"),
])
def test_scent_and_colour_variants_share_a_key(a, b):
    assert gg.variant_key(a) == gg.variant_key(b)


@pytest.mark.parametrize("a, b", [
    ("منظف الارضيات 3 لتر - ليمون", "منظف الارضيات 1 لتر - ليمون"),  # another size
    ("مزيل بقع للملابس البيضاء 1 لتر", "مزيل البقع للملابس الملونة 1 لتر"),  # not a trailing scent
    ("مبيض ومعقم (كلور) متعدد الاستخدامات - 4 لتر", "مبيض ومعقم سائل (كلور) - 1 لتر"),
])
def test_different_products_have_different_keys(a, b):
    assert gg.variant_key(a) != gg.variant_key(b)


@pytest.mark.parametrize("title", [
    "مزيل البقع على الجاف من Go Green - سعة 1 لتر",  # the part after '-' has a digit: kept
    "شامبو غسيل السجاد من GO GREEN - تنظيف وتعقيم (2 لتر)",  # a bracket: kept
])
def test_a_trailing_part_with_a_size_is_part_of_the_name(title):
    assert "لتر" in gg.variant_key(title) and "-" in gg.variant_key(title)


# --- the distinct-product cap -----------------------------------------------------------------------
def test_five_scents_of_one_product_count_once_and_the_cap_goes_on(monkeypatch):
    visited = _site(monkeypatch, FLOOR_SCENTS + OTHER_FLOOR)
    scraper = gg.GoGreenScraper(FakeBrowser(), AllRelevant())
    rows = scraper.scrape("floor cleaner", "volume")
    assert scraper.products_to_find_limit == 5
    assert [r["Product"] for r in rows] == [FLOOR_SCENTS[0][1]] + [t for _, t, _ in OTHER_FLOOR[:4]]
    # the other scents' pages are never opened: the variant is known from the catalog title
    assert not any(BASE + href in visited for href, _, _ in FLOOR_SCENTS[1:])
    assert BASE + OTHER_FLOOR[4][0] not in visited  # the cap of 5 distinct products was reached


def test_same_size_and_same_price_is_one_product(monkeypatch):
    items = [("/products/a", "مزيل بقع للملابس البيضاء 1 لتر", "18"),
             ("/products/b", "مزيل البقع للملابس الملونة 1 لتر", "18"),  # same 1 L, same 18 SAR
             ("/products/c", "مزيل البقع على الجاف من Go Green - سعة 1 لتر", "23"),
             ("/products/d", "مزيل بقع 500 مل", "18")]  # same price, another size: distinct
    visited = _site(monkeypatch, items)
    rows = gg.GoGreenScraper(FakeBrowser(), AllRelevant()).scrape("stain remover", "volume")
    assert [r["Product"] for r in rows] == [items[0][1], items[2][1], items[3][1]]
    assert BASE + "/products/b" in visited  # the price is only known from its page


def test_a_variant_at_another_price_is_still_a_variant_of_the_kept_one(monkeypatch):
    # 2026-11-25: the 'shell' scent of the 350 ml hand soap was listed at 132 SAR (a carton price)
    items = [("/products/s1", "صابون ايدي مضاد للبكتيريا 350 مل - ورد", "14"),
             ("/products/s2", "صابون ايدي مضاد للبكتيريا 350 مل - صدف", "132"),
             ("/products/s3", "صابون ايدي مضاد للبكتيريا 350 مل -مسك", "14")]
    _site(monkeypatch, items)
    rows = gg.GoGreenScraper(FakeBrowser(), AllRelevant()).scrape("hand soap", "volume")
    assert [(r["Product"], r["Price_SAR"]) for r in rows] == [(items[0][1], "14")]


def test_a_failed_first_variant_lets_the_next_variant_in(monkeypatch):
    _site(monkeypatch, FLOOR_SCENTS[:2],
          pages={"/products/f1": br.HttpStatusError(500, "f1")})
    rows = gg.GoGreenScraper(FakeBrowser(), AllRelevant()).scrape("floor cleaner", "volume")
    assert [r["Product"] for r in rows] == [FLOOR_SCENTS[1][1]]


def test_variants_are_counted_per_search(monkeypatch):
    _site(monkeypatch, FLOOR_SCENTS[:2])
    scraper = gg.GoGreenScraper(FakeBrowser(), AllRelevant())
    assert len(scraper.scrape("floor cleaner", "volume")) == 1
    assert len(scraper.scrape("floor cleaner", "volume")) == 1  # the next search starts afresh


def test_every_page_failing_still_fails_the_search(monkeypatch):
    _site(monkeypatch, FLOOR_SCENTS[:2], pages={"/products/f1": br.HttpStatusError(500, "f1"),
                                                "/products/f2": br.HttpStatusError(500, "f2")})
    with pytest.raises(br.HttpStatusError):
        gg.GoGreenScraper(FakeBrowser(), AllRelevant()).scrape("floor cleaner", "volume")


# --- the product page's own name ---------------------------------------------------------------------
@pytest.mark.parametrize("page_name, title, size, expected", [
    ("مطهر و معقم ضد الجراثيم 5 لتر", "مطهر و معقم ضد الجراثيم 750 مل", (750.0, "ml", 750.0), True),
    ("كرتون جل معقم لليدين 12 حبة 350 مل", "جل معقم لليدين - 350 مل", (350.0, "ml", 350.0), True),
    ("كرتون صابون ايدي 350 مل", "صابون ايدي مضاد للبكتيريا 350 مل - صدف", (350.0, "ml", 350.0), True),
    ("مطهر و معقم ضد الجراثيم 750 مل", "مطهر و معقم ضد الجراثيم 750 مل", (750.0, "ml", 750.0), False),
    ("مطهر و معقم ضد الجراثيم ٧٥٠ مل", "مطهر و معقم ضد الجراثيم 750 مل", (750.0, "ml", 750.0), False),
    ("مطهر و معقم ضد الجراثيم", "مطهر و معقم ضد الجراثيم 750 مل", (750.0, "ml", 750.0), False),  # no size
    ("منظف 1 لتر و 500 مل", "منظف 1 لتر", (1.0, "L", 1000.0), False),  # two sizes: says nothing
    ("كرتون مطهر و معقم الاسطح 4 حبة (5لتر)", "كرتون مطهر و معقم الاسطح 4 حبة (5لتر)",
     (20.0, "L", 20000.0), False),  # the catalog title states the carton too
    ("x", "منظف 1 لتر", (1.0, "L", 1000.0), False),
    # Drop when in doubt: a catalog pack of four 5 L bottles whose page states only "5 لتر" may be
    # one bottle at the pack price, so it is a contradiction.
    ("مطهر و معقم الاسطح 5 لتر", "مطهر و معقم الاسطح 4 حبة (5 لتر)", (20.0, "L", 20000.0), True),
    # The plural كراتين is a carton too.
    ("مطهر 4 كراتين 5 لتر", "مطهر 5 لتر", (5.0, "L", 5000.0), True),
])
def test_page_contradicts_catalog(page_name, title, size, expected):
    quantity, unit, normalized = size
    size = {"quantity": quantity, "unit": unit, "normalized": normalized}
    assert gg.page_contradicts_catalog(page_name, title, size) is expected


def test_a_page_that_states_another_size_is_dropped_and_the_cap_goes_on(monkeypatch):
    items = [("/products/a", "مطهر و معقم ضد الجراثيم 750 مل", "220"),
             ("/products/b", "مطهر ومعقم الأسطح GOGREEN لحماية موثوقة - 750 مل", "26")]
    _site(monkeypatch, items, pages={"/products/a": _product("220", "مطهر و معقم ضد الجراثيم 5 لتر")})
    scraper = gg.GoGreenScraper(FakeBrowser(), AllRelevant())
    scraper.products_to_find_limit = 1
    rows = scraper.scrape("surface disinfectant", "volume")
    assert [(r["Product"], r["Price_SAR"]) for r in rows] == [(items[1][1], "26")]


def test_parse_product_page_keeps_the_contract_when_the_page_agrees():
    size = {"quantity": 1.0, "unit": "L", "normalized": 1000.0}
    row = gg.parse_product_page(_product("1,182", "منظف 1 لتر"), "u", "منظف 1 لتر", size)
    assert row == {"Product": "منظف 1 لتر", "Price_SAR": "1182", "Company": "GoGreen", "URL": "u",
                   "Unit of measurement": "L", "Total quantity": 1.0}
    assert gg.parse_product_page(_product("10", "منظف 4 لتر"), "u", "منظف 1 لتر", size) is None
