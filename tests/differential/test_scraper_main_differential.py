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

A second test proves the Selenium side of ticket A did not move: `mumzworld_scraper.py`,
`saco_scraper.py` and `utils.py` are identical to the base, and so are the Mumzworld / SACO
exclusion lists and every TARGET_MAP entry except the 'noon' additions.

If the base ref does not exist locally the tests FAIL (`git fetch origin`).
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
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
from tests.differential import _harness as dh
from tests.e2e import _helpers as h

pytestmark = pytest.mark.differential

ANALYSIS = REPO_ROOT / "scraper" / "analysis-odoo.csv"
SITES = ("amazon", "mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense")
NEW_SITES = SITES + ("noon",)
APIFY_SITES = ("amazon", "noon")
CLASSES = {"AmazonScraper": "amazon", "MumzworldScraper": "mumzworld", "SacoScraper": "saco",
           "FineScraper": "fine", "GoGreenScraper": "gogreen", "OfficeSupplyScraper": "officesupply",
           "AeroSenseScraper": "aerosense"}
FAKE_TIME = types.SimpleNamespace(strftime=lambda fmt, *a: "2026-11-14", sleep=lambda s: None, time=time.time)
ISOLATED_PREFIXES = ("config", "utils", "scrapers", "services")
# Everything Mumzworld and SACO depend on besides config.py (checked value by value below):
# their scrapers, the shared parsers and the relevance agent they call.
UNCHANGED_FILES = ("scraper/scrapers/mumzworld_scraper.py", "scraper/scrapers/saco_scraper.py", "scraper/utils.py",
                   "scraper/services/ai_service.py")


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


def _materialize_base(tmp: Path) -> Path:
    ref = dh.base_ref()
    try:
        return h.materialize_ref(ref, tmp / "base")
    except RuntimeError as error:
        pytest.fail(f"cannot extract '{ref}' (git fetch origin?): {error}", pytrace=False)


def _run_base(base_root: Path, tmp: Path, out: Path) -> None:
    script = base_root / "scraper" / "main.py"
    source = script.read_text(encoding="utf-8")
    with isolated_scraper_tree(base_root):
        if re.search(r"^def main\(argv", source, re.MULTILINE):
            # F2 shape and later: importing has no side effects and `main(argv, ...)` takes the fakes.
            spec = importlib.util.spec_from_file_location("pi_base_scraper_main", script)
            base = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(base)
            assert Path(base.config.__file__).resolve() == (base_root / "scraper" / "config.py").resolve(), \
                "the base must run with its own config"
            with mock.patch.object(base, "time", FAKE_TIME):
                code = base.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite",
                                  "--output_file", str(out), "--coverage_file", str(tmp / "base_coverage.csv")],
                                 agent_factory=FakeAgent,
                                 scraper_factory=lambda d, a: {s: FakeScraper(s) for s in SITES},
                                 driver_installer=lambda: "/fake/chromedriver", sleep=lambda s: None)
            assert code == 0, f"base scraper/main.py returned {code!r}"
            return
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


def _run_new(out: Path, cov: Path) -> int:
    new = load_module("scraper/main.py")
    with mock.patch.object(new, "time", FAKE_TIME):
        return new.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite", "--output_file", str(out),
                         "--coverage_file", str(cov)],
                        agent_factory=FakeAgent,
                        scraper_factory=lambda d, a: {s: FakeScraper(s, with_new_keys=s in APIFY_SITES)
                                                      for s in NEW_SITES},
                        driver_installer=lambda: "/fake/chromedriver", sleep=lambda s: None,
                        apify_preflight=lambda: None)


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def test_same_scraper_outputs_give_the_same_rows_mumzworld_and_saco_included(tmp_path):
    base_root = _materialize_base(tmp_path)
    base_out, new_out = tmp_path / "base.csv", tmp_path / "new.csv"
    _run_base(base_root, tmp_path, base_out)
    assert _run_new(new_out, tmp_path / "coverage.csv") == 0
    base_header, base_rows = _read(base_out)
    new_header, new_rows = _read(new_out)

    assert new_header == base_header + ["seller", "origin"]
    for source in SITES:
        b = [r for r in base_rows if r["source"] == source]
        n = [{k: r[k] for k in base_header} for r in new_rows if r["source"] == source]
        assert n == b, f"{source} rows changed on the base columns ({len(b)} base, {len(n)} new)"
    for source in ("mumzworld", "saco"):
        n = [r for r in new_rows if r["source"] == source]
        assert n, f"the test would prove nothing: no {source} rows"
        assert all((r["seller"], r["origin"]) == ("", "unknown") for r in n), f"{source} got a seller or origin"
    for source in ("fine", "gogreen", "officesupply", "aerosense"):
        assert all((r["seller"], r["origin"]) == ("", "unknown") for r in new_rows if r["source"] == source)
    amazon = [r for r in new_rows if r["source"] == "amazon"]
    assert amazon and all(r["seller"].startswith("Seller") for r in amazon), "Apify Seller reaches the CSV"
    assert {r["source"] for r in base_rows} <= set(SITES)
    assert not [r for r in base_rows if r["source"] == "noon"]
    assert [r for r in new_rows if r["source"] == "noon"], "noon rows exist only in the new version"
    assert len(new_rows) == len(base_rows) + sum(r["source"] == "noon" for r in new_rows)

    _, cov = _read(tmp_path / "coverage.csv")
    assert sum(int(r["rows_written"]) for r in cov) == len(new_rows)
    assert any(r["error"] == "TimeoutError" and r["site"] == "saco" for r in cov), "the failing keyword was exercised"
    assert {r["subindustry"] for r in cov if r["site"] == "noon"} <= {"Home", "Automotive", "Pets"}


def test_selenium_scrapers_parsers_and_exclusions_are_unchanged(tmp_path):
    ref = dh.base_ref()
    if h.resolve_ref(ref) is None:
        pytest.fail(f"the ref '{ref}' does not exist locally (git fetch origin?)", pytrace=False)
    diff = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--quiet", ref, "--", *UNCHANGED_FILES],
                          capture_output=True, text=True)
    assert diff.returncode == 0, f"ticket A: these files must stay identical to '{ref}': {UNCHANGED_FILES}"

    base_root = _materialize_base(tmp_path)
    spec = importlib.util.spec_from_file_location("pi_base_scraper_config", base_root / "scraper" / "config.py")
    base_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base_config)
    new_config = load_module("scraper/main.py").config
    assert new_config.MUMZWORLD_EXCLUSIONS == base_config.MUMZWORLD_EXCLUSIONS
    assert new_config.SACO_EXCLUSIONS == base_config.SACO_EXCLUSIONS
    assert new_config.USER_AGENT == base_config.USER_AGENT  # used by both Selenium scrapers
    assert set(new_config.TARGET_MAP) == set(base_config.TARGET_MAP)
    for key, base_sites in base_config.TARGET_MAP.items():
        assert [s for s in new_config.TARGET_MAP[key] if s != "noon"] == base_sites, f"TARGET_MAP[{key!r}] changed"
    assert new_config.CSV_COLUMNS[:len(base_config.CSV_COLUMNS)] == base_config.CSV_COLUMNS
