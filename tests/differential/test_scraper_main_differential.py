"""C15 / F6 regression of `scraper/main.py`: same scraper outputs → identical CSV rows, base vs new.

F2 rewrote `scraper/main.py` (temp file + rename, fail loudly, coverage report, helpers extracted)
and the Mumzworld and SACO rows must not change (ticket A). Both versions run in-process over the
versioned `scraper/analysis-odoo.csv` (read only) with the SAME fake scrapers: each returns a
deterministic product list per (site, keyword, mode), and one site raises for one keyword, which
both versions must log and skip. Nothing opens Chrome or calls an AI: the base version gets its
`RelevanceAgent`, `ChromeDriverManager`, scraper classes and `time` replaced in its globals when it
has the pre-F2 shape (e.g. 8c278ca), or injected through `main(argv, ...)` when it has the F2 shape
(e.g. the base after the F3 cutover); the new one gets them injected. The date is pinned so both
write the same day.

The base version (`PI_BASE_REF`, default `origin/server-improvement`) is read with `git show`; if
the ref does not exist locally the test FAILS (`git fetch origin`).
"""
from __future__ import annotations

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

pytestmark = pytest.mark.differential

ANALYSIS = REPO_ROOT / "scraper" / "analysis-odoo.csv"
SITES = ("amazon", "mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense")
CLASSES = {"AmazonScraper": "amazon", "MumzworldScraper": "mumzworld", "SacoScraper": "saco",
           "FineScraper": "fine", "GoGreenScraper": "gogreen", "OfficeSupplyScraper": "officesupply",
           "AeroSenseScraper": "aerosense"}
FAKE_TIME = types.SimpleNamespace(strftime=lambda fmt, *a: "2026-11-14", sleep=lambda s: None, time=time.time)


class FakeScraper:
    """Deterministic: 0-3 products per (site, keyword, mode), from a hash. Raises for one keyword."""

    def __init__(self, site):
        self.site = site

    def scrape(self, keyword, mode):
        h = int(hashlib.sha256(f"{self.site}|{keyword}|{mode}".encode()).hexdigest(), 16)
        if self.site == "saco" and h % 11 == 0:
            raise TimeoutError("fake selenium timeout")
        return [{"Product": f"{self.site} {keyword} #{i}", "Price_SAR": round(5 + (h >> i) % 9000 / 100, 2),
                 "Company": f"Brand{(h >> (i + 3)) % 7}", "URL": f"https://{self.site}.example.invalid/{h % 997}/{i}",
                 "Unit of measurement": ["ml", "L", "fl oz", "units"][(h >> i) % 4],
                 "Total quantity": 1 + (h >> (i + 5)) % 1000} for i in range(h % 4)]


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


def _base_main_source() -> str:
    ref = dh.base_ref()
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{ref}:scraper/main.py"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"cannot read scraper/main.py from '{ref}' (git fetch origin?): {r.stderr.strip()}", pytrace=False)
    return r.stdout


def _run_base(tmp: Path, out: Path) -> None:
    new = load_module("scraper/main.py")  # puts scraper/ on sys.path and loads config, scrapers, ai_service
    source = _base_main_source()
    script = tmp / "base" / "scraper" / "main.py"
    script.parent.mkdir(parents=True)
    script.write_text(source, encoding="utf-8")
    saved = list(sys.path)
    if re.search(r"^def main\(argv", source, re.MULTILINE):
        # F2 shape and later (e.g. the base after the F3 cutover): importing has no side effects and
        # `main(argv, ...)` takes the fakes, exactly like the new version.
        try:
            spec = importlib.util.spec_from_file_location("pi_base_scraper_main", script)
            base = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(base)
            assert base.config.CSV_COLUMNS == new.config.CSV_COLUMNS
            with mock.patch.object(base, "time", FAKE_TIME):
                code = base.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite",
                                  "--output_file", str(out), "--coverage_file", str(tmp / "base_coverage.csv")],
                                 agent_factory=FakeAgent,
                                 scraper_factory=lambda d, a: {s: FakeScraper(s) for s in SITES},
                                 driver_installer=lambda: "/fake/chromedriver", sleep=lambda s: None)
            assert code == 0, f"base scraper/main.py returned {code!r}"
        finally:
            sys.path[:] = saved
        return
    # Pre-F2 shape (e.g. 8c278ca): argv is parsed at import time and the collaborators are module globals.
    argv = [str(script), "--analysis_file", str(ANALYSIS), "--output_mode", "overwrite", "--output_file", str(out)]
    try:
        with mock.patch.object(sys, "argv", argv):
            g = runpy.run_path(str(script), run_name="pi_base_scraper_main")  # module level parses argv
        real_globals = g["main"].__globals__
        real_globals["RelevanceAgent"] = FakeAgent
        real_globals["ChromeDriverManager"] = lambda: types.SimpleNamespace(install=lambda: "/fake/chromedriver")
        for cls, site in CLASSES.items():
            real_globals[cls] = (lambda s: (lambda driver_path, relevance_agent=None: FakeScraper(s)))(site)
        real_globals["time"] = FAKE_TIME
        assert real_globals["config"].CSV_COLUMNS == new.config.CSV_COLUMNS
        real_globals["main"]()
    finally:
        sys.path[:] = saved


def _run_new(out: Path, cov: Path) -> int:
    new = load_module("scraper/main.py")
    with mock.patch.object(new, "time", FAKE_TIME):
        return new.main(["--analysis_file", str(ANALYSIS), "--output_mode", "overwrite", "--output_file", str(out),
                         "--coverage_file", str(cov)],
                        agent_factory=FakeAgent, scraper_factory=lambda d, a: {s: FakeScraper(s) for s in SITES},
                        driver_installer=lambda: "/fake/chromedriver", sleep=lambda s: None)


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_same_scraper_outputs_give_identical_rows_mumzworld_and_saco_included(tmp_path):
    base_out, new_out = tmp_path / "base.csv", tmp_path / "new.csv"
    _run_base(tmp_path, base_out)
    assert _run_new(new_out, tmp_path / "coverage.csv") == 0
    base_rows, new_rows = _read(base_out), _read(new_out)
    for source in ("mumzworld", "saco"):
        b = [r for r in base_rows if r["source"] == source]
        n = [r for r in new_rows if r["source"] == source]
        assert b, f"the test would prove nothing: no {source} rows in the base output"
        assert n == b, f"{source} rows changed ({len(b)} base, {len(n)} new)"
    assert new_rows == base_rows
    assert new_out.read_bytes() == base_out.read_bytes()
    # The coverage report accounts for every row written.
    cov = _read(tmp_path / "coverage.csv")
    assert sum(int(r["rows_written"]) for r in cov) == len(new_rows)
    assert any(r["error"] == "TimeoutError" and r["site"] == "saco" for r in cov), "the failing keyword was exercised"
