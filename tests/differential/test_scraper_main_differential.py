"""C15 / F6 regression of `scraper/main.py`: same scraper outputs → same CSV rows, base vs new.

Ticket A: the Mumzworld and SACO rows must not change. Both versions run in-process over the
versioned `scraper/analysis-odoo.csv` (read only) with the SAME fake scrapers: each returns a
deterministic product list per (site, keyword, mode), and one site raises for one keyword, which
both versions must log and skip. Nothing opens Chrome or calls an AI or Apify. The date is pinned so
both write the same day.

The base version (`PI_BASE_REF`, default `origin/server-improvement`) runs with ITS OWN `scraper/`
(config, scrapers, services, utils), extracted with `git archive` into a temporary directory and
imported in isolation (`sys.modules` and `sys.path` are saved and restored), so the comparison
holds when the new tree deletes or adds scraper modules (F6 deletes `amazon_scraper.py`). The base
gets its collaborators replaced in its globals when it has the pre-F2 shape (e.g. 8c278ca), or
injected through `main(argv, ...)` when it has the F2 shape.

F6 changes the output on purpose (Mafer, 2026-11-22): two columns are appended (`seller`,
`origin`) and Noon is a new site in Home, Automotive and Pets. So the new CSV is compared with the
base one projected onto the base header, source by source and in the same order; full byte
equality no longer applies (REVIEW rule 7: the per-source equality on the 13 original columns, plus
the new checks below, is at least as strict for Mumzworld and SACO as before).

Since F6 was merged into `server-improvement` (2026-11-23) the base itself can have the F6 shape
(`main(..., apify_preflight=...)`, 15 columns, Noon in TARGET_MAP). Then both versions run with the
same F6 fakes and the CSVs must be identical byte for byte; the per-source projection above only
applies to a pre-F6 base.

A second test proves the shared side of ticket A did not move: `utils.py` and `ai_service.py` are
identical to the base, and so are the Mumzworld / SACO exclusion lists and every TARGET_MAP entry
except the 'noon' additions.

Since the Playwright migration (Mafer, 2026-11-25, option A: "solo navegador") the Mumzworld and
SACO scrapers are no longer byte-identical to a Selenium base: only their browser layer changed.
Instead of the file check, the saved-HTML tests run the BASE scrapers (Selenium, through
`_fake_selenium`, no browser) and the NEW ones (Playwright, through a fake browser) on the same
saved search and product pages and require the same rows and the same relevance-AI calls, plus a
Hypothesis property over product titles for the product-page parsers. When the base is already a
Playwright version (after this change is merged) the same tests still compare base and new.

The removed and added sites (Mumzworld out of Home, GoGreen into Home, both 2026-11-25) only count
while the base still has, or still lacks, them: against a base that already has the change the
whole CSV and every TARGET_MAP entry must match, with no exception left.

Selenium is not installed any more: the base `scraper/main.py` and scrapers import the fake one.

If the base ref does not exist locally the tests FAIL (`git fetch origin`).
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import inspect
import re
import runpy
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from tests.conftest import REPO_ROOT, load_module
from hypothesis import given, strategies as st

from tests.differential import _fake_selenium
from tests.differential import _harness as dh
from tests.e2e import _helpers as h

pytestmark = pytest.mark.differential

ANALYSIS = REPO_ROOT / "scraper" / "analysis-odoo.csv"
SITES = ("amazon", "mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense")
# Sites removed from TARGET_MAP on purpose, with the decision (docs/decisions.md):
# Mumzworld, 2026-11-25 (Mafer: "quita Mumzworld", HTTP 403 to every automated client).
REMOVED_SITES = {"Home": ("mumzworld",)}
REMOVED_SOURCES = {"mumzworld"}
# Sites added on purpose: GoGreen in Home, 2026-11-25 (Mafer: GoGreen is more B2C; Home keeps
# sizes up to 4 L and searches it with the general keyword).
ADDED_SITES = {"Home": ("gogreen",)}


def _added(row):
    return row["source"] in ADDED_SITES.get(row["subindustry"], ())
NEW_SITES = SITES + ("noon",)
APIFY_SITES = ("amazon", "noon")
CLASSES = {"AmazonScraper": "amazon", "MumzworldScraper": "mumzworld", "SacoScraper": "saco",
           "FineScraper": "fine", "GoGreenScraper": "gogreen", "OfficeSupplyScraper": "officesupply",
           "AeroSenseScraper": "aerosense"}
FAKE_TIME = types.SimpleNamespace(strftime=lambda fmt, *a: "2026-11-14", sleep=lambda s: None, time=time.time)
ISOLATED_PREFIXES = ("config", "utils", "scrapers", "services")
# Everything Mumzworld and SACO depend on besides config.py (checked value by value below):
# their scrapers, the shared parsers and the relevance agent they call.
UNCHANGED_FILES = ("scraper/utils.py", "scraper/services/ai_service.py")
FAKE_SELENIUM_PREFIXES = ("selenium", "webdriver_manager")


class FakeScraper:
    """Deterministic: 0-3 products per (site, keyword, mode), from a hash. Raises for one keyword.
    With ``with_new_keys`` (only the Apify sites in the new version) it adds Seller and Origin."""

    def __init__(self, site, with_new_keys=False):
        self.site = site
        self.with_new_keys = with_new_keys

    def scrape(self, keyword, mode):
        h_ = int(hashlib.sha256(f"{self.site}|{keyword}|{mode}".encode()).hexdigest(), 16)
        if self.site == "saco" and h_ % 11 == 0:
            raise TimeoutError("fake selenium timeout")
        products = [{"Product": f"{self.site} {keyword} #{i}", "Price_SAR": round(5 + (h_ >> i) % 9000 / 100, 2),
                     "Company": f"Brand{(h_ >> (i + 3)) % 7}",
                     "URL": f"https://{self.site}.example.invalid/{h_ % 997}/{i}",
                     "Unit of measurement": ["ml", "L", "fl oz", "units"][(h_ >> i) % 4],
                     "Total quantity": 1 + (h_ >> (i + 5)) % 1000} for i in range(h_ % 4)]
        if self.with_new_keys:
            for i, p in enumerate(products):
                p["Seller"] = f"Seller{(h_ >> (i + 7)) % 5}"
                p["Origin"] = ["local", "imported", "unknown"][(h_ >> (i + 9)) % 3]
        return products


class FakeAgent:
    fatal_error = None

    def preflight(self):
        pass

    def stats(self):
        return {"evaluated": 0, "relevant": 0, "failures": 0, "wipes_failures": 0}

    def failure_rate(self):
        return 0.0

    def check_failure_rate(self, threshold=0.05):
        pass


def _is_isolated(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in ISOLATED_PREFIXES)


@contextlib.contextmanager
def isolated_scraper_tree(root: Path):
    """Imports inside the block resolve `config`, `utils`, `scrapers.*` and `services.*` from
    ``root/scraper`` (the base tree). The test `log_config` stub stays. On exit `sys.modules` and
    `sys.path` are restored, so the new version's modules are untouched for the other tests."""
    load_module("scraper/main.py")  # the new modules and the log_config stub are loaded first
    saved_path = list(sys.path)
    saved_modules = {name: mod for name, mod in sys.modules.items() if _is_isolated(name)}
    for name in saved_modules:
        del sys.modules[name]
    sys.path[:0] = [str(root / "scraper"), str(root)]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _is_isolated(n)]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


@contextlib.contextmanager
def fake_selenium_installed(site=None):
    """While inside, `selenium` and `webdriver_manager` are the fakes of `_fake_selenium` serving
    ``site`` (URL -> HTML); the previous modules (real or absent) are restored on exit."""
    def ours(name):
        return any(name == p or name.startswith(p + ".") for p in FAKE_SELENIUM_PREFIXES)
    saved = {name: mod for name, mod in sys.modules.items() if ours(name)}
    for name in saved:
        del sys.modules[name]
    _fake_selenium.install(site or {})
    try:
        yield
    finally:
        for name in [n for n in sys.modules if ours(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


def _materialize_base(tmp: Path) -> Path:
    ref = dh.base_ref()
    try:
        return h.materialize_ref(ref, tmp / "base")
    except RuntimeError as error:
        pytest.fail(f"cannot extract '{ref}' (git fetch origin?): {error}", pytrace=False)


def _base_config(base_root: Path):
    """The base `scraper/config.py`, loaded on its own (it imports nothing from the tree)."""
    spec = importlib.util.spec_from_file_location("pi_base_scraper_config", base_root / "scraper" / "config.py")
    base_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base_config)
    return base_config


def _sites_still_removed(base_map):
    """REMOVED_SITES the base still scrapes (a base that already removed them has nothing to filter)."""
    return {k: tuple(s for s in v if s in base_map.get(k, ())) for k, v in REMOVED_SITES.items()}


def _sites_still_added(base_map):
    """ADDED_SITES the base does not scrape yet (a base that already has them must match fully)."""
    return {k: tuple(s for s in v if s not in base_map.get(k, ())) for k, v in ADDED_SITES.items()}


def _run_base(base_root: Path, tmp: Path, out: Path) -> bool:
    """Runs the base scraper into ``out``. Returns True when the base already has the F6 shape."""
    script = base_root / "scraper" / "main.py"
    source = script.read_text(encoding="utf-8")
    with isolated_scraper_tree(base_root), fake_selenium_installed():
        if re.search(r"^def main\(argv", source, re.MULTILINE):
            # F2 shape and later: importing has no side effects and `main(argv, ...)` takes the fakes.
            spec = importlib.util.spec_from_file_location("pi_base_scraper_main", script)
            base = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(base)
            assert Path(base.config.__file__).resolve() == (base_root / "scraper" / "config.py").resolve(), \
                "the base must run with its own config"
            is_f6 = "apify_preflight" in inspect.signature(base.main).parameters
            kwargs = dict(agent_factory=FakeAgent,
                          scraper_factory=lambda d, a: {s: FakeScraper(s) for s in SITES},
                          sleep=lambda s: None)
            if "browser_factory" in inspect.signature(base.main).parameters:  # Playwright base
                kwargs["browser_factory"] = lambda: "/fake/browser"
            else:  # Selenium base
                kwargs["driver_installer"] = lambda: "/fake/chromedriver"
            if is_f6:
                # F6 shape: same fakes as the new version, and no real Apify preflight.
                kwargs.update(scraper_factory=lambda d, a: {s: FakeScraper(s, with_new_keys=s in APIFY_SITES)
                                                            for s in NEW_SITES},
                              apify_preflight=lambda: None)
            with mock.patch.object(base, "time", FAKE_TIME):
                code = base.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite",
                                  "--output_file", str(out), "--coverage_file", str(tmp / "base_coverage.csv")],
                                 **kwargs)
            assert code == 0, f"base scraper/main.py returned {code!r}"
            return is_f6
        # Pre-F2 shape (e.g. 8c278ca): argv is parsed at import time and the collaborators are globals.
        argv = [str(script), "--analysis_file", str(ANALYSIS), "--output_mode", "overwrite", "--output_file", str(out)]
        with mock.patch.object(sys, "argv", argv):
            g = runpy.run_path(str(script), run_name="pi_base_scraper_main")
        real_globals = g["main"].__globals__
        real_globals["RelevanceAgent"] = FakeAgent
        real_globals["ChromeDriverManager"] = lambda: types.SimpleNamespace(install=lambda: "/fake/chromedriver")
        for cls, site in CLASSES.items():
            real_globals[cls] = (lambda s: (lambda driver_path, relevance_agent=None: FakeScraper(s)))(site)
        real_globals["time"] = FAKE_TIME
        real_globals["main"]()
        return False


def _run_new(out: Path, cov: Path) -> int:
    new = load_module("scraper/main.py")
    with mock.patch.object(new, "time", FAKE_TIME):
        return new.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite", "--output_file", str(out),
                         "--coverage_file", str(cov)],
                        agent_factory=FakeAgent,
                        scraper_factory=lambda d, a: {s: FakeScraper(s, with_new_keys=s in APIFY_SITES)
                                                      for s in NEW_SITES},
                        browser_factory=lambda: "/fake/browser", sleep=lambda s: None,
                        apify_preflight=lambda: None)


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def test_same_scraper_outputs_give_the_same_rows_except_the_removed_and_added_sites(tmp_path):
    base_root = _materialize_base(tmp_path)
    base_out, new_out = tmp_path / "base.csv", tmp_path / "new.csv"
    base_is_f6 = _run_base(base_root, tmp_path, base_out)
    assert _run_new(new_out, tmp_path / "coverage.csv") == 0
    base_header, base_rows = _read(base_out)
    new_header, new_rows = _read(new_out)
    base_map = _base_config(base_root).TARGET_MAP
    # Mafer, 2026-11-25: Mumzworld leaves TARGET_MAP (HTTP 403 to every automated client). The
    # new version writes no Mumzworld rows; a base that still scrapes it writes some, and every
    # other row is unchanged.
    assert not [r for r in new_rows if r["source"] in REMOVED_SOURCES], "Mumzworld is still scraped"
    still_removed = _sites_still_removed(base_map)
    removed = [r for r in base_rows if r["source"] in still_removed.get(r["subindustry"], ())]
    if any(still_removed.values()):
        assert removed, "the test would prove nothing: the base wrote no Mumzworld rows"
    base_rows = [r for r in base_rows if r not in removed]
    # GoGreen in Home (2026-11-25): against a base without it, rows only the new version writes;
    # against a base that has it, the same rows on both sides.
    assert [r for r in new_rows if _added(r)], "the new version wrote no GoGreen rows in Home"
    assert all(r["channel"] == "B2C" for r in new_rows if _added(r))
    still_added = _sites_still_added(base_map)
    added = [r for r in new_rows if r["source"] in still_added.get(r["subindustry"], ())]
    assert not [r for r in base_rows if r["source"] in still_added.get(r["subindustry"], ())], \
        "the base wrote rows for a site its TARGET_MAP does not have"
    new_rows = [r for r in new_rows if r not in added]

    if base_is_f6:
        # The base already has F6: same fakes, so every other row must not change at all.
        assert new_header == base_header
        assert new_rows == base_rows, "the CSV changed against an F6 base (sites removed or added since aside)"
    else:
        assert new_header == base_header + ["seller", "origin"]
        for source in [s for s in SITES if s not in REMOVED_SOURCES]:
            b = [r for r in base_rows if r["source"] == source]
            n = [{k: r[k] for k in base_header} for r in new_rows if r["source"] == source]
            assert n == b, f"{source} rows changed on the base columns ({len(b)} base, {len(n)} new)"
    for source in ("saco",):
        n = [r for r in new_rows if r["source"] == source]
        assert n, f"the test would prove nothing: no {source} rows"
        assert all((r["seller"], r["origin"]) == ("", "unknown") for r in n), f"{source} got a seller or origin"
    for source in ("fine", "gogreen", "officesupply", "aerosense"):
        assert all((r["seller"], r["origin"]) == ("", "unknown") for r in new_rows if r["source"] == source)
    amazon = [r for r in new_rows if r["source"] == "amazon"]
    assert amazon and all(r["seller"].startswith("Seller") for r in amazon), "Apify Seller reaches the CSV"
    assert [r for r in new_rows if r["source"] == "noon"], "the new version writes noon rows"
    if not base_is_f6:
        assert {r["source"] for r in base_rows} <= set(SITES) - REMOVED_SOURCES
        assert not [r for r in base_rows if r["source"] == "noon"], "noon rows exist only in the new version"
        assert len(new_rows) == len(base_rows) + sum(r["source"] == "noon" for r in new_rows)

    _, cov = _read(tmp_path / "coverage.csv")
    assert sum(int(r["rows_written"]) for r in cov) == len(new_rows) + len(added)
    assert any(r["error"] == "TimeoutError" and r["site"] == "saco" for r in cov), "the failing keyword was exercised"
    assert {r["subindustry"] for r in cov if r["site"] == "noon"} <= {"Home", "Automotive", "Pets"}


def test_shared_parsers_ai_and_exclusions_are_unchanged(tmp_path):
    ref = dh.base_ref()
    if h.resolve_ref(ref) is None:
        pytest.fail(f"the ref '{ref}' does not exist locally (git fetch origin?)", pytrace=False)
    diff = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--quiet", ref, "--", *UNCHANGED_FILES],
                          capture_output=True, text=True)
    assert diff.returncode == 0, f"ticket A: these files must stay identical to '{ref}': {UNCHANGED_FILES}"

    base_root = _materialize_base(tmp_path)
    base_config = _base_config(base_root)
    new_config = load_module("scraper/main.py").config
    assert new_config.MUMZWORLD_EXCLUSIONS == base_config.MUMZWORLD_EXCLUSIONS
    assert new_config.SACO_EXCLUSIONS == base_config.SACO_EXCLUSIONS
    assert new_config.USER_AGENT == base_config.USER_AGENT  # the base Selenium scrapers sent it
    assert set(new_config.TARGET_MAP) == set(base_config.TARGET_MAP)
    base_has_noon = any("noon" in sites for sites in base_config.TARGET_MAP.values())
    still_removed = _sites_still_removed(base_config.TARGET_MAP)
    still_added = _sites_still_added(base_config.TARGET_MAP)
    for key, base_sites in base_config.TARGET_MAP.items():
        new_sites = new_config.TARGET_MAP[key]
        assert not set(REMOVED_SITES.get(key, ())) & set(new_sites), f"TARGET_MAP[{key!r}] has a removed site"
        assert set(ADDED_SITES.get(key, ())) <= set(new_sites), f"TARGET_MAP[{key!r}] lost an added site"
        base_sites = [s for s in base_sites if s not in still_removed.get(key, ())]
        new_sites = [s for s in new_sites if s not in still_added.get(key, ())]
        if base_has_noon:  # the base already has F6: no site may change at all
            assert new_sites == base_sites, f"TARGET_MAP[{key!r}] changed"
        else:
            assert [s for s in new_sites if s != "noon"] == base_sites, f"TARGET_MAP[{key!r}] changed"
    assert new_config.CSV_COLUMNS[:len(base_config.CSV_COLUMNS)] == base_config.CSV_COLUMNS


# --- C15 after the Playwright migration: same saved HTML, same Mumzworld and SACO rows ---------------
MUMZ = "https://www.mumzworld.com/sa-en/"
SACO = "https://www.saco.sa/en/"


def _mumz_search(products):
    cards = "".join(f'<div class="ProductCard_productCard__x1Ab"><a class="ProductCard_productName__q9" '
                    f'href="{href}">{href}</a></div>' for href in products)
    return f"<html><body><div>{cards}</div></body></html>"


def _mumz_product(name, price="1,234"):
    title = f'<h1 class="ProductDetails_productName__lcVK_">{name}</h1>' if name is not None else "<h2>gone</h2>"
    price_html = f'<span class="Price_integer__z1">{price}</span>' if price is not None else ""
    return f"<html><body>{title}<div>{price_html}<span class='Price_decimal__z'>.50</span></div></body></html>"


MUMZ_TITLES = [
    "Pigeon - Baby Bottle Cleaner 500ml", "Johnson's Baby Shampoo 300 ml Pack of 3", "Dettol Kids Wash 250ml x 2",
    "Mustela Cleansing Water 1L", "Some Toy Car", "Chicco - Bottle Wash 1 Liter", "Huggies - Baby Wipes 56 wipes Pack of 3",
    "Pampers Sensitive 52 count", "WaterWipes 60s", "Mothercare Laundry Liquid 2 L of 2", "Sebamed Baby Lotion 200 ml",
    "Toy Brush Set", "Philips Avent Bottle Cleaner 3 x 250 ml",
] + [f"Brand{n} - Bottle Wash {n * 100} ml" for n in range(1, 7)] + [f"Brand{n} Baby Wipes {n * 10} wipes" for n in range(1, 9)]
MUMZ_SITE = {f"{MUMZ}search?q={k}": _mumz_search([f"/sa-en/p{i}" for i in range(len(MUMZ_TITLES))] + ["/sa-en/p0"])
             for k in ("bottle%20cleaner", "baby%20wipes")}
MUMZ_SITE.update({f"{MUMZ}p{i}": _mumz_product(t, price=None if i == 3 else f"{i},0{i}") for i, t in enumerate(MUMZ_TITLES)})
MUMZ_SITE[f"{MUMZ}p5"] = _mumz_product(None)  # a product page whose title never appears: skipped by both
MUMZ_SEARCHES = [("bottle cleaner", "volume"), ("baby wipes", "units")]


def _saco_search(hrefs):
    cards = "".join(f'<div><a href="{h}"><img/></a><a href="{h}">title</a></div>' for h in hrefs)
    return f"<html><body>{cards}<a href='/en/category/x'>cat</a></body></html>"


def _saco_jsonld(name, price=None, brand=None, offers_as_list=False, extra_invalid=False):
    data = {"@type": "Product", "name": name}
    if price is not None:
        offer = {"price": price}
        data["offers"] = [offer] if offers_as_list else offer
    if brand is not None:
        data["brand"] = {"name": brand}
    invalid = '<script type="application/ld+json">{not json</script>' if extra_invalid else ""
    other = '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
    import json as _json
    return (f"<html><head>{invalid}{other}<script type=\"application/ld+json\">{_json.dumps(data)}</script></head>"
            f"<body><h1>{name} (h1)</h1></body></html>")


SACO_PRODUCTS = [
    _saco_jsonld("DAC Floor Cleaner 4.5 Liter", 24.95, "DAC"),
    _saco_jsonld("Bona Hardwood Floor Cleaner 946ml", "79", "Bona", offers_as_list=True),
    _saco_jsonld("Dettol Floor Cleaner 1.8 L", 0, None, extra_invalid=True),
    "<html><body><h1>Clorox Bleach 3.78 L</h1></body></html>",  # no JSON-LD: the h1 is the title
    _saco_jsonld("Mop Bucket", 59, "Vileda"),
    _saco_jsonld("Fine Tissue 200 Sheets x 5", 15.5, "Fine"),
    _saco_jsonld("Zewa Kitchen Towels 4 Rolls", 22, "Zewa"),
    _saco_jsonld("Harpic Toilet Cleaner 750 ml", 12.75, "Harpic"),
    _saco_jsonld("Pril Dish Liquid 1 L", 9.5, "Pril"),
]
SACO_SITE = {f"{SACO}products-search?q={k}": _saco_search(
    [f"/en/product/p{i}" for i in range(len(SACO_PRODUCTS))] + [f"{SACO}product/p0"])
    for k in ("floor%20cleaner", "tissue")}
SACO_SITE.update({f"{SACO}product/p{i}": html for i, html in enumerate(SACO_PRODUCTS)})
SACO_SEARCHES = [("floor cleaner", "volume"), ("tissue", "units")]


class RecordingAgent:
    """Relevance AI stand-in: relevant unless the title says toy or bucket; records every call."""

    def __init__(self):
        self.calls = []

    def is_relevant(self, title, keyword):
        self.calls.append((title, keyword))
        return not any(word in title.lower() for word in ("toy", "bucket"))


class SavedPage:
    def __init__(self):
        self.url, self.html = "about:blank", ""

    def content(self):
        return self.html

    def wait_for_timeout(self, ms):
        pass


class SavedBrowser:
    @contextlib.contextmanager
    def page(self):
        yield SavedPage()


def _saved_fetch(site, browser_module):
    """The new scrapers' ``fetch_html`` over saved pages: 404 for an unknown URL and
    ``ElementNotFound`` when no part of ``wait_for`` is in the page (``:has-text`` by its text)."""
    from bs4 import BeautifulSoup

    def fetch(page, url, wait_for=None, timeout_ms=None):
        if url not in site:
            raise browser_module.HttpStatusError(404, url)
        page.url, page.html = url, site[url]
        if wait_for:
            soup = BeautifulSoup(page.html, "html.parser")
            found = False
            for part in (p.strip() for p in wait_for.split(", ")):
                if ":has-text(" in part:
                    found |= part.split(":has-text(", 1)[1].strip("')\"") in soup.get_text(" ")
                else:
                    found |= soup.select_one(part) is not None
            if not found:
                raise browser_module.ElementNotFound(f"{wait_for} at {url}")
        return page.html
    return fetch


def _base_scraper_modules(base_root: Path):
    """The base Mumzworld and SACO modules, imported from the base tree with fake Selenium. The
    module objects keep working after the context exits (their names were bound at import)."""
    with isolated_scraper_tree(base_root), fake_selenium_installed(MUMZ_SITE | SACO_SITE):
        mumz = importlib.import_module("scrapers.mumzworld_scraper")
        saco = importlib.import_module("scrapers.saco_scraper")
    for module in (mumz, saco):
        if hasattr(module, "time"):
            module.time = FAKE_TIME  # SACO slept 2-3 s around the cookie banner and the results
    return mumz, saco


def _run_scraper(scraper_cls, browser_arg, searches):
    agent = RecordingAgent()
    scraper = scraper_cls(browser_arg, relevance_agent=agent)
    rows = {f"{k}|{mode}": scraper.scrape(k, mode) for k, mode in searches}
    return rows, agent.calls


def _run_new_scrapers(monkeypatch):
    load_module("scraper/main.py")
    from scrapers import mumzworld_scraper, saco_scraper
    from services import browser as browser_module
    for module in (mumzworld_scraper, saco_scraper):
        monkeypatch.setattr(module, "fetch_html", _saved_fetch(MUMZ_SITE | SACO_SITE, browser_module))
    return mumzworld_scraper, saco_scraper


@pytest.fixture(scope="module")
def base_modules(tmp_path_factory):
    return _base_scraper_modules(_materialize_base(tmp_path_factory.mktemp("c15")))


@pytest.mark.parametrize("site", ["mumzworld", "saco"])
def test_mumzworld_and_saco_give_the_same_rows_from_the_same_saved_html(base_modules, monkeypatch, site):
    base_mumz, base_saco = base_modules
    new_mumz, new_saco = _run_new_scrapers(monkeypatch)
    if site == "mumzworld":
        base_cls, new_cls, searches = base_mumz.MumzworldScraper, new_mumz.MumzworldScraper, MUMZ_SEARCHES
    else:
        base_cls, new_cls, searches = base_saco.SacoScraper, new_saco.SacoScraper, SACO_SEARCHES
    # A Selenium base takes a driver path; a Playwright base (after the merge) takes the browser.
    base_arg = SavedBrowser() if "browser" in inspect.signature(base_cls.__init__).parameters else "/fake/chromedriver"
    if base_arg != "/fake/chromedriver":
        for module in base_modules:
            # The base `services.browser` (its own exception classes, which its collect_products
            # catches), not the new one that `sys.modules` holds again after the isolated import.
            base_browser = types.SimpleNamespace(**module.fetch_html.__globals__)
            monkeypatch.setattr(module, "fetch_html", _saved_fetch(MUMZ_SITE | SACO_SITE, base_browser))
    base_rows, base_calls = _run_scraper(base_cls, base_arg, searches)
    new_rows, new_calls = _run_scraper(new_cls, SavedBrowser(), searches)
    assert new_rows == base_rows
    assert new_calls == base_calls, "the relevance AI sees the same titles in the same order"
    assert all(rows for rows in base_rows.values()), "the test would prove nothing: a search gave no rows"
    limit = 10 if site == "mumzworld" else 6
    assert any(len(rows) == limit for rows in base_rows.values()), "the product limit was exercised"


def test_saco_no_results_page_is_an_empty_search_and_mumzworld_without_cards_raises(monkeypatch):
    new_mumz, new_saco = _run_new_scrapers(monkeypatch)
    from services import browser as browser_module
    site = {f"{SACO}products-search?q=zzq": '<html><body><h2 class="text-lg">No results found!</h2></body></html>',
            f"{MUMZ}search?q=zzq": "<html><body>blocked</body></html>"}
    monkeypatch.setattr(new_saco, "fetch_html", _saved_fetch(site, browser_module))
    monkeypatch.setattr(new_mumz, "fetch_html", _saved_fetch(site, browser_module))
    assert new_saco.SacoScraper(SavedBrowser(), RecordingAgent()).scrape("zzq", "volume") == []
    with pytest.raises(browser_module.ElementNotFound):
        new_mumz.MumzworldScraper(SavedBrowser(), RecordingAgent()).scrape("zzq", "volume")


TITLE_WORDS = st.sampled_from(["Baby", "Bottle", "Cleaner", "-", "Wash", "Pack", "of", "x", "X", "Liquid", "Gel",
                               "Pigeon", "Johnson's", "Toy", "Sensitive", "(", ")", "+", "&", "Fragrance"])
TITLE_SIZES = st.builds(lambda n, unit, sep: f"{n}{sep}{unit}",
                        st.sampled_from(["1", "2", "3", "12", "56", "250", "500", "1.5", "0.75", "1,000"]),
                        st.sampled_from(["ml", "ML", "L", "l", "Liter", "litre", "Ltr", "wipes", "count", "sheets",
                                         "pcs", "pieces", "s", "pack", "Pack", "oz", "fl oz", "g", "kg"]),
                        st.sampled_from(["", " "]))
TITLES = st.lists(st.one_of(TITLE_WORDS, TITLE_SIZES, st.builds(lambda n: f"Pack of {n}", st.integers(1, 24)),
                            st.builds(lambda n: f"x {n}", st.integers(1, 24))), min_size=1, max_size=8).map(" ".join)


@given(title=TITLES, mode=st.sampled_from(["volume", "units"]),
       price=st.sampled_from(["12", "1,234", "0", "99.50"]))
def test_product_page_parsers_match_the_base_for_any_title(base_modules, title, mode, price):
    base_mumz, base_saco = base_modules
    new_mumz = sys.modules["scrapers.mumzworld_scraper"]
    new_saco = sys.modules["scrapers.saco_scraper"]
    url = "https://example.invalid/p"
    mumz_html = _mumz_product(title, price)
    saco_html = _saco_jsonld(title, price, "Brand")
    if hasattr(base_mumz.MumzworldScraper, "_extract_product_details"):  # Selenium base
        driver = _fake_selenium.FakeDriver({url: mumz_html})
        base_m = base_mumz.MumzworldScraper("/fake", None)._extract_product_details(driver, url, mode)
        driver = _fake_selenium.FakeDriver({url: saco_html})
        base_s = base_saco.SacoScraper("/fake", None)._extract_product_details(driver, url, mode)
    else:  # Playwright base
        base_m = base_mumz.parse_product_page(mumz_html, url, mode)
        base_s = base_saco.parse_product_page(saco_html, url, mode)
    assert new_mumz.parse_product_page(mumz_html, url, mode) == base_m
    assert new_saco.parse_product_page(saco_html, url, mode) == base_s
