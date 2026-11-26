"""Unit tests of the versioned keyword mapping ``scraper/modifiers_mapping.csv`` (2026-11-25).

Mafer's decisions of 2026-11-25:
- AeroSense "Flushman Lavatory Additive" (aircraft toilet-tank fluid) and "Cabin Disinfectant
  Concentrate" (must be diluted) are not comparable with Wayakit's ready-to-use products, and
  "Airframe cleaner" is not a glass cleaner: their keywords are removed (A4-Lavatory disinfectant is
  left without modifiers, like the other A4 types, so AeroSense is not searched for it);
- laundry stain removers do not count for the B2B "Stain spot remover" types (carpets and
  upholstery) and chlorine bleach is not a broad-spectrum disinfectant: GoGreen's title rules
  (``gogreen_scraper.query_rule_rejects``) and the relevance AI only see the search query, so
  GoGreen is searched with "carpet and upholstery stain remover" and "surface disinfectant" for
  those types; the other sites keep their keywords.

The mapping is read through the same code the pipeline uses (``build_analysis_list`` and
``scraper/main.py``'s ``parse_instruction`` / ``run_scraping``) with fake scrapers: no browser, no
network, no AI.
"""
from __future__ import annotations

import pandas as pd
import pytest

from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
conn = load_module("scraper/odoo_api_connection_products.py")

MAPPING = REPO_ROOT / "scraper" / "modifiers_mapping.csv"
REMOVED = ("aerosense:airframe cleaner", "aerosense:cabin disinfectant concentrate",
           "aerosense:flushman lavatory additive")


@pytest.fixture(scope="module")
def mapping():
    return pd.read_csv(MAPPING)


def _modifiers(mapping, type_of_product):
    [value] = mapping.loc[mapping["Type of product"] == type_of_product, "Search Modifiers"].tolist()
    return "" if pd.isna(value) else value


def _plan(mapping, type_of_product):
    return m.parse_instruction({"Type of product": type_of_product, "Generic product type": "x",
                                "Search Modifiers": mapping.set_index("Type of product")
                                .loc[type_of_product, "Search Modifiers"]})


def test_the_mapping_keeps_its_shape(mapping):
    assert list(mapping.columns) == ["Type of product", "Search Modifiers"]
    assert not mapping["Type of product"].duplicated().any()


def test_the_three_aerosense_keywords_are_gone(mapping):
    text = MAPPING.read_text(encoding="utf-8").lower()
    for keyword in REMOVED:
        assert keyword not in text, keyword
    for word in ("airframe", "flushman", "concentrate"):
        assert word not in text, word


def test_the_other_aerosense_keywords_of_those_types_are_kept(mapping):
    assert _modifiers(mapping, "A2-Broad-spectrum disinfectant for surfaces") == \
        "aerosense:cabin disinfectant ready use"
    for glass in ("A3-Streak-free window cleaner", "A3-Cockpit glass cleaner", "A3-Mirror and plexiglass cleaner"):
        assert _plan(mapping, glass)["site_specific_keywords"] == {"aerosense": ["windshield cleaner"]}


def test_the_lavatory_disinfectant_row_is_empty_like_the_other_a4_rows(mapping):
    empty = _plan(mapping, "A4-Lavatory disinfectant")
    other = _plan(mapping, "A4-Toilet and urinal cleaner")
    for plan in (empty, other):
        assert plan["site_specific_keywords"] == {} and plan["general_modifiers"] == []
    assert empty["search_keyword"] == "lavatory disinfectant"
    products = pd.DataFrame([{"Type_of_product": "A4-Lavatory disinfectant", "SubIndustry": "Aviation",
                              "Industry": "Transportation", "Generic product type": "Disinfectant and cleaner"}])
    [row] = conn.build_analysis_list(products, mapping).to_dict("records")
    assert row["Search Modifiers"] == ""


class _Recorder:
    def __init__(self):
        self.calls = []

    def scrape(self, keyword, mode):
        self.calls.append((keyword, mode))
        return []


class _Agent:
    fatal_error = None

    def stats(self):
        return {"evaluated": 0, "relevant": 0, "failures": 0, "wipes_failures": 0}


class _Writer:
    def writerows(self, rows):
        raise AssertionError("no rows expected")


def _searches(mapping, rows):
    """Runs ``run_scraping`` over the analysis list built from the real mapping for ``rows``
    ((type, subindustry, industry)) with recording scrapers; returns {site: [keywords]}."""
    products = pd.DataFrame([{"Type_of_product": t, "SubIndustry": s, "Industry": i,
                              "Generic product type": "x"} for t, s, i in rows])
    analysis = conn.build_analysis_list(products, mapping)
    # the pipeline writes the list to CSV and main.py reads it back: an empty cell becomes NaN
    analysis["Search Modifiers"] = analysis["Search Modifiers"].replace("", float("nan"))
    recorders = {}

    def factory(browser, agent):
        for site in ("aerosense", "fine", "gogreen", "officesupply", "amazon", "noon", "saco"):
            recorders.setdefault(site, _Recorder())
        return recorders

    coverage = []
    m.run_scraping(analysis, _Writer(), _Agent(), factory, None, coverage, sleep=lambda s: None)
    return {site: [k for k, _ in r.calls] for site, r in recorders.items() if r.calls}


def test_aerosense_is_not_searched_for_the_lavatory_disinfectant(mapping):
    searches = _searches(mapping, [("A4-Lavatory disinfectant", "Aviation", "Transportation"),
                                   ("A3-Cockpit glass cleaner", "Aviation", "Transportation")])
    assert searches == {"aerosense": ["windshield cleaner"]}


STAIN_SPOT_REMOVERS = ["F2-FM stain spot remover", "I3-Stain spot remover", "S1-Stain spot remover",
                       "L1-Stain spot remover", "E2-Stain spot remover", "U2-Stain spot remover"]
BROAD_SPECTRUM = ["F3-FM broad-spectrum disinfectant for surfaces and touchpoints",
                  "I4-Broad-spectrum disinfectant for surfaces, shoe racks and storage areas",
                  "S3-Broad-spectrum disinfectant for surfaces",
                  "L2-Broad-spectrum disinfectant for spas and salons equipment",
                  "E3-Broad-spectrum disinfectant for surfaces, mattress and touchpoints",
                  "E13-Broad-spectrum disinfectant for spas and salons equipment",
                  "U4-Broad-spectrum disinfectants for sanitizing work areas and break rooms "]


def test_every_b2b_stain_spot_remover_searches_gogreen_for_carpets_and_upholstery(mapping):
    for type_of_product in STAIN_SPOT_REMOVERS:
        assert _plan(mapping, type_of_product)["site_specific_keywords"] == {
            "gogreen": ["carpet and upholstery stain remover"]}, type_of_product
    # no other B2B stain spot remover sends GoGreen a query without carpets or upholstery
    for type_of_product in mapping["Type of product"]:
        if "stain spot remover" in type_of_product.lower():
            for keyword in _plan(mapping, type_of_product)["site_specific_keywords"].get("gogreen", []):
                assert "carpet" in keyword or "upholstery" in keyword, type_of_product


def test_every_broad_spectrum_disinfectant_searches_gogreen_for_surface_disinfectants(mapping):
    for type_of_product in BROAD_SPECTRUM:
        assert _plan(mapping, type_of_product)["site_specific_keywords"] == {
            "fine": ["Disinfectant"], "gogreen": ["surface disinfectant"],
            "officesupply": ["disinfectant"]}, type_of_product


def test_the_b2b_queries_reach_gogreen_and_the_other_sites_unchanged(mapping):
    searches = _searches(mapping, [("S1-Stain spot remover", "Gyms", "Facilities management"),
                                   ("S3-Broad-spectrum disinfectant for surfaces", "Gyms", "Facilities management")])
    assert searches == {"gogreen": ["carpet and upholstery stain remover", "surface disinfectant"],
                        "fine": ["Disinfectant"], "officesupply": ["disinfectant"]}
