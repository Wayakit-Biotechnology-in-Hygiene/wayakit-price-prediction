"""Unit tests of GoGreen's title rules for disinfectant and stain remover searches (2026-11-25).

Mafer's decisions: chlorine bleach is not a broad-spectrum (surface) disinfectant competitor; a
carpet shampoo is not a disinfectant even if its title says sanitizing (تعقيم); laundry stain
removers do not count for carpet and upholstery stain or spot removers. They are applied in
``gogreen_scraper.query_rule_rejects``, before the relevance AI and in GoGreen only: the shared
relevance prompt (``ai_service.py``), which SACO also uses, stays identical to the base (ticket A;
checked by ``tests/differential/test_scraper_main_differential.py``).

No browser, no network, no AI: the catalog and product pages are hand-written HTML served by a fake
``fetch_html`` and the relevance agent is a fake batch agent. Arabic titles are real GoGreen catalog
titles of the 2026-11-25 run.
"""
from __future__ import annotations

import contextlib

import pandas as pd
import pytest

from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")  # puts scraper/ on sys.path, like the other scraper tests
from scrapers import gogreen_scraper as gg  # noqa: E402
from services import browser as br  # noqa: E402

BASE = "https://gogreen.com.sa"
CATALOG = BASE + "/products?page={}"

BLEACH_AR = "مبيض ومعقم (كلور) متعدد الاستخدامات - 4 لتر"
CARPET_SHAMPOO_AR = "شامبو غسيل السجاد من GO GREEN - تنظيف وتعقيم (2 لتر)"
SURFACE_DISINFECTANT_AR = "مطهر ومعقم الأسطح GOGREEN لحماية موثوقة - 750 مل"
LAUNDRY_STAIN_AR = "مزيل بقع للملابس البيضاء 1 لتر"
CARPET_STAIN_AR = "مزيل بقع السجاد والمفروشات 500 مل"


@pytest.mark.parametrize("query, title, rejected", [
    # (a) chlorine bleach for a disinfectant query
    ("surface disinfectant", BLEACH_AR, True),
    ("surface disinfectant", "منظف ومبيض الأسطح 1 لتر", True),
    ("surface disinfectant", "مطهر الأسطح بالكلور 1 لتر", True),
    ("surface disinfectant", "معقم هيبوكلوريت الصوديوم 1 لتر", True),
    ("surface disinfectant spray", "Clorox Disinfecting Bleach, Concentrated Formula, 1.4 L", True),
    ("disinfectant wipes", "Disinfecting Wipes with Bleach, 75 Wipes", True),
    ("food-safe surface sanitizers", "Chlorine Sanitizer Tablets Liquid 1 L", True),
    ("chlorine bleach", BLEACH_AR, False),  # the query asks for bleach
    ("bleach", "Clorox Disinfecting Bleach, 1.4 L", False),  # not a disinfectant query
    ("disinfectant wipes", "Clorox Disinfecting Wipes, Bleach Free, Lemon Scent, 75 Wipes", False),
    ("surface disinfectant", "مطهر الأسطح بدون كلور 1 لتر", False),
    ("surface disinfectant", "منظف و مطهر الاسطح بدون عطر ولون 5 لتر", False),
    ("surface disinfectant", SURFACE_DISINFECTANT_AR, False),
    ("surface disinfectant", "مطهر بنزالكونيوم كلوريد 1 لتر", False),  # chloride is not chlorine
    ("surface disinfectant", "كلوركس مطهر متعدد الاستخدامات 1 لتر", True),  # Clorox
    ("surface disinfectant", "معقم ومنظف وللكلور فعالية 1 لتر", True),  # و + لل + كلور
    ("surface disinfectant", "سائل للكلور والتعقيم 1 لتر", True),  # لل + كلور
    ("surface disinfectant", "معقم هيبوكلورايت الصوديوم 1 لتر", True),
    ("surface disinfectant", "مبيّض ومعقم الأسطح 1 لتر", True),  # shadda
    ("surface disinfectant", "مبيـــض ومعقم الأسطح 1 لتر", True),  # tatweel
    ("surface disinfectant", "مطهر كلوريد البنزالكونيوم المركّز 1 لتر", False),  # still chloride, with a shadda
    # (b) a carpet or upholstery product for a disinfectant query
    ("surface disinfectant", CARPET_SHAMPOO_AR, True),
    ("broad-spectrum disinfectant for surfaces", "Carpet Shampoo, Cleans and Sanitizes, 2 L", True),
    ("surface disinfectant spray", "منظف ومعقم المفروشات والكنب 500 مل", True),
    ("surface disinfectant", "مطهر ومعقم للأسطح والأثاث 1 لتر", False),  # furniture is not upholstery
    ("surface disinfectant", "معقم أقمشة الأثاث 500 مل", True),  # furniture fabric is
    ("surface disinfectant", "معقم التنجيد والستائر 500 مل", True),
    ("carpet shampoo", CARPET_SHAMPOO_AR, False),  # not a disinfectant query
    # (c) a laundry stain remover for a carpet or upholstery stain query
    ("carpet and upholstery stain remover", LAUNDRY_STAIN_AR, True),
    ("spot remover for carpets", "Oxi Action Fabric Stain Remover for Colored Clothes, 1 L", True),
    ("carpet and upholstery stain remover", "مزيل بقع الغسيل للملابس الملونة 1 لتر", True),
    ("carpet and upholstery stain remover", CARPET_STAIN_AR, False),
    ("carpet and upholstery stain remover", CARPET_SHAMPOO_AR, False),  # غسيل السجاد is for carpets
    ("carpet and upholstery stain remover", "مزيل بقع للملابس والسجاد 1 لتر", False),  # names carpets
    ("stain remover for laundry", LAUNDRY_STAIN_AR, False),  # the laundry type keeps them
    ("stain remover on the go", LAUNDRY_STAIN_AR, False),
    # other searches are not touched
    ("floor cleaner", BLEACH_AR, False),
    ("glass cleaner", LAUNDRY_STAIN_AR, False),
])
def test_query_rule_rejects(query, title, rejected):
    assert (gg.query_rule_rejects(title, query) is not None) is rejected


def test_arabic_indic_digits_and_empty_values_do_not_break_the_rules():
    assert gg.query_rule_rejects("مبيض ومعقم (كلور) ٤ لتر", "surface disinfectant")
    assert gg.query_rule_rejects("", "surface disinfectant") is None
    assert gg.query_rule_rejects(BLEACH_AR, "") is None
    assert gg.query_rule_rejects(BLEACH_AR, None) is None


# --- the mapping's GoGreen queries trigger the rules ----------------------------------------------
def _gogreen_queries(predicate):
    mapping = pd.read_csv(REPO_ROOT / "scraper" / "modifiers_mapping.csv")
    queries = set()
    for _, row in mapping.iterrows():
        if predicate(row["Type of product"].lower()):
            plan = m.parse_instruction({"Type of product": row["Type of product"], "Generic product type": "x",
                                        "Search Modifiers": row["Search Modifiers"]})
            queries.update(plan["site_specific_keywords"].get("gogreen", []))
    return queries


def test_every_b2b_stain_spot_remover_query_drops_laundry_stain_removers():
    queries = _gogreen_queries(lambda t: "stain spot remover" in t)
    assert queries == {"carpet and upholstery stain remover"}
    for query in queries:
        assert gg.query_rule_rejects(LAUNDRY_STAIN_AR, query)
        assert gg.query_rule_rejects(CARPET_STAIN_AR, query) is None


def test_every_broad_spectrum_disinfectant_query_drops_bleach_and_carpet_shampoo():
    queries = _gogreen_queries(lambda t: "broad-spectrum disinfectant" in t)
    assert queries == {"surface disinfectant"}
    for query in queries:
        assert gg.query_rule_rejects(BLEACH_AR, query)
        assert gg.query_rule_rejects(CARPET_SHAMPOO_AR, query)
        assert gg.query_rule_rejects(SURFACE_DISINFECTANT_AR, query) is None


@pytest.mark.parametrize("type_of_product, title, rejected", [
    # Home (B2C) searches GoGreen with the general keyword, built from the type's name
    ("H8-Surface disinfectant spray", BLEACH_AR, True),
    ("H8-Surface disinfectant spray", CARPET_SHAMPOO_AR, True),
    ("H7-Spot remover for carpets", LAUNDRY_STAIN_AR, True),
    ("H7-Spot remover for carpets", CARPET_STAIN_AR, False),
    ("H9-Stain remover for laundry", LAUNDRY_STAIN_AR, False),
])
def test_home_general_keywords(type_of_product, title, rejected):
    plan = m.parse_instruction({"Type of product": type_of_product, "Generic product type": "x",
                                "Search Modifiers": float("nan")})
    assert (gg.query_rule_rejects(title, plan["search_keyword"]) is not None) is rejected


# --- the scraper applies them before the relevance AI ---------------------------------------------
class FakeBrowser:
    @contextlib.contextmanager
    def page(self):
        yield object()


class AllRelevant:
    def __init__(self):
        self.calls = []

    def filter_relevant(self, titles, query):
        self.calls.append((list(titles), query))
        return [True] * len(titles)


def _site(monkeypatch, items):
    boxes = "".join(f'<div class="product-box"><a class="tp-image-product" href="{href}">'
                    f'<img alt="{title}"/></a></div>' for href, title, _ in items)
    served = {CATALOG.format(1): f'<html lang="ar"><body>{boxes}</body></html>'}
    for href, title, price in items:
        served[BASE + href] = (f'<html lang="ar"><h1 class="title-product">x</h1>'
                               f'<p class="product-formatted-price">{price}</p></html>')
    visited = []

    def fetch(page, url, wait_for=None, timeout_ms=None):
        visited.append(url)
        if url not in served:
            raise br.HttpStatusError(404, url)
        return served[url]

    monkeypatch.setattr(gg, "fetch_html", fetch)
    return visited


ITEMS = [("/products/b", BLEACH_AR, "12"), ("/products/c", CARPET_SHAMPOO_AR, "25"),
         ("/products/s", SURFACE_DISINFECTANT_AR, "19"), ("/products/l", LAUNDRY_STAIN_AR, "14"),
         ("/products/k", CARPET_STAIN_AR, "22")]


def test_disinfectant_search_never_sends_bleach_or_carpet_shampoo_to_the_ai(monkeypatch):
    visited = _site(monkeypatch, ITEMS)
    agent = AllRelevant()
    products = gg.GoGreenScraper(FakeBrowser(), agent).scrape("surface disinfectant", "volume")
    [(titles, query)] = agent.calls
    assert query == "surface disinfectant"
    assert BLEACH_AR not in titles and CARPET_SHAMPOO_AR not in titles
    # the carpet stain remover is a carpet product too
    assert titles == [SURFACE_DISINFECTANT_AR, LAUNDRY_STAIN_AR]
    assert not {BASE + "/products/b", BASE + "/products/c", BASE + "/products/k"} & set(visited)
    assert [p["Product"] for p in products] == titles


def test_carpet_stain_search_never_sends_laundry_stain_removers_to_the_ai(monkeypatch):
    _site(monkeypatch, ITEMS)
    agent = AllRelevant()
    products = gg.GoGreenScraper(FakeBrowser(), agent).scrape("carpet and upholstery stain remover", "volume")
    [(titles, _)] = agent.calls
    assert LAUNDRY_STAIN_AR not in titles
    assert BLEACH_AR in titles and CARPET_SHAMPOO_AR in titles  # the AI judges the rest, as before
    assert LAUNDRY_STAIN_AR not in [p["Product"] for p in products]


def test_a_search_whose_titles_are_all_rejected_does_not_call_the_ai(monkeypatch):
    _site(monkeypatch, [ITEMS[0], ITEMS[1]])
    agent = AllRelevant()
    assert gg.GoGreenScraper(FakeBrowser(), agent).scrape("surface disinfectant", "volume") == []
    assert agent.calls == []


def test_other_searches_send_every_sized_title_to_the_ai(monkeypatch):
    _site(monkeypatch, ITEMS)
    agent = AllRelevant()
    gg.GoGreenScraper(FakeBrowser(), agent).scrape("floor cleaner", "volume")
    [(titles, _)] = agent.calls
    assert titles == [title for _, title, _ in ITEMS]
