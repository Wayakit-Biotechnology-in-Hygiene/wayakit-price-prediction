"""Unit tests of `scraper/main.py` after F2: temp file + rename (C02), fail loudly, coverage report.

`main()` runs in-process with everything injected: a fake relevance agent (or the real
`RelevanceAgent` with a fake Bedrock client), fake scrapers (no Selenium, no Chrome), a fake
ChromeDriver installer and a no-op sleep. All files live in `tmp_path`.

What it proves:
- success: the output CSV is replaced, no temp file is left, header + rows with `CSV_COLUMNS`;
- any failure (instructions missing, driver, preflight, fatal AI error even when a scraper
  swallows it, unexpected exception, 0 rows, AI failure rate above 5 %) → exit 1 and the previous
  CSV is byte-for-byte untouched; the coverage report is still written;
- append mode copies the previous rows and adds no second header;
- one `scrape()` exception is not fatal: logged, counted in the coverage `error` column;
- C08: `build_row_data` keys are exactly `config.CSV_COLUMNS` and survive a DictWriter round trip;
- the TARGET_MAP lookup and the Mumzworld/SACO exclusions are unchanged (C06 is not fixed here).
"""
from __future__ import annotations

import csv
import random
import sys

import pandas as pd
import pytest

from tests.conftest import load_module
from tests.unit._fake_bedrock import FakeBedrock

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
config = m.config
# The same ai_service module object main.py uses (load_module would create a second copy).
ai = sys.modules[m.RelevanceAgent.__module__]

OLD_CSV = "date,product\n2026-11-07,previous run row\n"
INSTRUCTIONS = [
    {"Type of product": "H1-Floor cleaner", "Sub industry": "Home", "Industry": "Home care",
     "Generic product type": "Floor cleaner", "Search Modifiers": ""},
    {"Type of product": "P1-Pet wipes", "Sub industry": "Pets", "Industry": "Pets",
     "Generic product type": "Wipes", "Search Modifiers": "amazon: pet wipes; amazon: dog wipes"},
    {"Type of product": "A1-Leather cleaner", "Sub industry": "Aviation", "Industry": "Transportation",
     "Generic product type": "Leather cleaner", "Search Modifiers": "aerosense: leather cleaner"},
]


class FakeAgent:
    def __init__(self, preflight_error=None, rate_error=False):
        self._stats = {"evaluated": 0, "relevant": 0, "failures": 0, "wipes_failures": 0}
        self.fatal_error = None
        self.preflight_error = preflight_error
        self.rate_error = rate_error

    def preflight(self):
        if self.preflight_error:
            raise self.preflight_error

    def is_relevant(self, title, query):
        self._stats["evaluated"] += 1
        ok = "good" in title
        self._stats["relevant"] += ok
        return ok

    def stats(self):
        return dict(self._stats)

    def failure_rate(self):
        return 0.0

    def check_failure_rate(self, threshold=0.05):
        if self.rate_error:
            raise m.RelevanceAgentError("fake: AI failure rate above 5 %")


class FakeScraper:
    """Returns two titles per search; with an agent, only the relevant one."""

    def __init__(self, site, agent, behavior=None):
        self.site, self.agent, self.behavior = site, agent, behavior
        self.calls = []

    def scrape(self, keyword, mode):
        self.calls.append((keyword, mode))
        if self.behavior:
            out = self.behavior(self, keyword, mode)
            if out is not None:
                return out
        products = [_product(self.site, keyword, "good"), _product(self.site, keyword, "other")]
        if self.agent is not None:
            products = [p for p in products if self.agent.is_relevant(p["Product"], keyword)]
        return products


def _product(site, keyword, tag):
    return {"Product": f"{tag} {site} {keyword}", "Price_SAR": 12.5, "Company": "Fake brand",
            "URL": f"https://{site}.example.invalid/{tag}", "Unit of measurement": "ml", "Total quantity": 500}


def _factory(behaviors=None, created=None):
    behaviors = behaviors or {}

    def factory(driver_path, agent):
        assert driver_path == "/fake/chromedriver"
        scrapers = {s: FakeScraper(s, None if s in m.SITES_WITHOUT_AI else agent, behaviors.get(s))
                    for s in ("amazon", "mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense")}
        if created is not None:
            created.append(scrapers)
        return scrapers
    return factory


@pytest.fixture
def paths(tmp_path):
    instr = tmp_path / "analysis.csv"
    pd.DataFrame(INSTRUCTIONS).to_csv(instr, index=False)
    return {"instr": instr, "out": tmp_path / "competitors_complete.csv", "cov": tmp_path / "cov" / "coverage.csv"}


def _main(paths, mode="overwrite", agent=None, factory=None, installer=None, sleeps=None):
    agent = agent or FakeAgent()
    argv = ["--analysis_file", str(paths["instr"]), "--output_mode", mode, "--output_file", str(paths["out"]),
            "--coverage_file", str(paths["cov"])]
    return m.main(argv, agent_factory=lambda: agent, scraper_factory=factory or _factory(),
                  driver_installer=installer or (lambda: "/fake/chromedriver"),
                  sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _coverage(paths):
    return _rows(paths["cov"])


# --- success ------------------------------------------------------------------------------------
def test_success_replaces_the_csv_and_leaves_no_temp(paths):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    sleeps = []
    assert _main(paths, sleeps=sleeps) == 0
    rows = _rows(paths["out"])
    assert list(rows[0]) == config.CSV_COLUMNS
    assert {r["source"] for r in rows} == {"amazon", "mumzworld", "saco", "aerosense"}
    assert all(r["product"].startswith("good") for r in rows if r["source"] != "aerosense")
    assert not (paths["out"].parent / "competitors_complete.tmp.csv").exists()
    assert sleeps == [m.SECONDS_BETWEEN_SUBINDUSTRIES] * 3
    channels = {(r["subindustry"], r["channel"]) for r in rows}
    assert channels == {("Home", "B2C"), ("Pets", "B2C"), ("Aviation", "B2B")}


def test_append_copies_the_previous_rows_without_a_second_header(paths):
    assert _main(paths) == 0
    first = paths["out"].read_text(encoding="utf-8")
    assert _main(paths, mode="append") == 0
    second = paths["out"].read_text(encoding="utf-8")
    assert second.startswith(first)
    assert second.count("date,industry,subindustry") == 1
    assert len(_rows(paths["out"])) == 2 * len(_rows_from_text(first))


def _rows_from_text(text):
    return list(csv.DictReader(text.splitlines()))


def test_append_without_a_previous_file_writes_the_header(paths):
    assert _main(paths, mode="append") == 0
    assert list(_rows(paths["out"])[0]) == config.CSV_COLUMNS


def test_temp_path_is_next_to_the_output_and_ignored_by_git():
    assert m.temp_path_for("scraper/competitors_complete.csv") == "scraper/competitors_complete.tmp.csv"
    assert m.temp_path_for("/x/out") == "/x/out.tmp"
    assert m.temp_path_for("a.b/c.csv").endswith(".csv")


def test_parse_args_defaults():
    args = m.parse_args([])
    assert (args.analysis_file, args.output_mode, args.output_file, args.coverage_file) == (
        config.INSTRUCTIONS_FILE, "overwrite", config.OUTPUT_CSV_FILE, "scraper/coverage_report.csv")


# --- failures keep the previous CSV ---------------------------------------------------------------
def _boom(*a, **k):
    raise RuntimeError("fake unexpected failure")


FAILURES = {
    "driver": dict(installer=_boom),
    "preflight": dict(agent=FakeAgent(preflight_error=m.RelevanceAgentFatalError("fake: no access"))),
    "factory": dict(factory=_boom),
    "zero-rows": dict(factory=_factory({s: (lambda self, k, mode: []) for s in
                                        ("amazon", "mumzworld", "saco", "aerosense")})),
    "failure-rate": dict(agent=FakeAgent(rate_error=True)),
}


@pytest.mark.parametrize("mode", ["overwrite", "append"])
@pytest.mark.parametrize("case", FAILURES)
def test_failures_exit_1_and_keep_the_previous_csv(paths, case, mode):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    assert _main(paths, mode=mode, **FAILURES[case]) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    assert paths["cov"].is_file(), "the coverage report is written on every exit"


def test_missing_instructions_exit_1_and_keep_the_previous_csv(paths):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    paths["instr"].unlink()
    assert _main(paths) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV


def test_fatal_ai_error_swallowed_by_a_scraper_still_aborts(paths):
    """The Selenium scrapers catch every exception; main must look at agent.fatal_error."""
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    agent = FakeAgent()

    def swallow(self, keyword, mode):
        agent.fatal_error = m.RelevanceAgentFatalError("fake AccessDeniedException")
        return []  # what a scraper returns after catching the exception

    created = []
    assert _main(paths, agent=agent, factory=_factory({"mumzworld": swallow}, created)) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    cov = _coverage(paths)
    assert cov[-1]["site"] == "mumzworld" and cov[-1]["error"] == "RelevanceAgentFatalError"
    assert cov[-1]["rows_written"] == "0"
    assert created[0]["saco"].calls == [], "nothing is scraped after the fatal error"


def test_fatal_ai_error_raised_by_a_scraper_aborts(paths):
    def raise_fatal(self, keyword, mode):
        raise m.RelevanceAgentFatalError("fake ExpiredTokenException")

    assert _main(paths, factory=_factory({"amazon": raise_fatal})) == 1
    assert not paths["out"].exists()


def test_one_site_error_is_logged_counted_and_does_not_abort(paths):
    def flaky(self, keyword, mode):
        raise TimeoutError("fake selenium timeout")

    assert _main(paths, factory=_factory({"saco": flaky})) == 0
    rows = _rows(paths["out"])
    assert "saco" not in {r["source"] for r in rows} and "mumzworld" in {r["source"] for r in rows}
    saco = [r for r in _coverage(paths) if r["site"] == "saco"]
    assert saco and all(r["error"] == "TimeoutError" and r["rows_written"] == "0" for r in saco)


# --- coverage -----------------------------------------------------------------------------------
def test_coverage_report_counts_found_relevant_and_rows(paths):
    assert _main(paths) == 0
    cov = _coverage(paths)
    assert list(cov[0]) == m.COVERAGE_COLUMNS
    keys = [(r["subindustry"], r["type_of_product"], r["site"], r["keyword"]) for r in cov]
    assert len(keys) == len(set(keys))
    pets = [r for r in cov if r["subindustry"] == "Pets"]
    assert [r["keyword"] for r in pets] == ["pet wipes", "dog wipes"]
    for r in cov:
        if r["site"] == "aerosense":  # no AI: everything it returns is found and relevant
            assert r["found"] == r["relevant"] == r["rows_written"] == "2"
        else:
            assert (r["found"], r["relevant"], r["rows_written"], r["ai_failures"]) == ("2", "1", "1", "0")
    assert sum(int(r["rows_written"]) for r in cov) == len(_rows(paths["out"]))


def test_coverage_with_the_real_agent_and_a_fake_bedrock(paths):
    """Integration with RelevanceAgent: the stats deltas reach the report (no AWS)."""
    agent = ai.RelevanceAgent(client=FakeBedrock(), sleep=lambda s: None, rng=random.Random(0))
    assert _main(paths, agent=agent) == 0
    cov = _coverage(paths)
    home_amazon = [r for r in cov if r["site"] == "amazon" and r["subindustry"] == "Home"]
    assert [(r["found"], r["relevant"], r["ai_failures"]) for r in home_amazon] == [("2", "1", "0")]


def test_coverage_entry_without_ai_uses_rows_written():
    delta = {"evaluated": 9, "relevant": 4, "failures": 1, "wipes_failures": 2}
    assert m.coverage_entry("S", "T", "aerosense", "k", delta, 3, uses_ai=False)["found"] == 3
    e = m.coverage_entry("S", "T", "amazon", "k", delta, 3, error="TimeoutError")
    assert (e["found"], e["relevant"], e["ai_failures"], e["error"]) == (9, 4, 3, "TimeoutError")


def test_zero_relevant_pairs():
    report = [m.coverage_entry("S", "T", "amazon", "k", {"evaluated": 3, "relevant": 0}, 0),
              m.coverage_entry("S", "T", "saco", "k", {"evaluated": 3, "relevant": 2}, 2)]
    assert [r["site"] for r in m.zero_relevant_pairs(report)] == ["amazon"]


# --- C08 / unchanged rules ----------------------------------------------------------------------
def test_row_data_keys_are_csv_columns_and_round_trip(tmp_path):
    product = _product("saco", "floor cleaner", "good")
    row = m.build_row_data(product, "Home care", "Home", "H1-Floor cleaner", "Floor cleaner", "saco", "B2C",
                           "2026-11-14")
    assert list(row) == config.CSV_COLUMNS
    p = tmp_path / "x.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)  # extrasaction='raise', as in main.py
        w.writeheader()
        w.writerow(row)
    assert _rows(p) == [{k: str(v) for k, v in row.items()}]


def test_csv_columns_are_unchanged():
    assert config.CSV_COLUMNS == ["date", "industry", "subindustry", "type_of_product", "generic_product_type",
                                  "product", "price_sar", "company", "source", "url", "unit_of_measurement",
                                  "total_quantity", "channel"]


def test_target_map_lookup_is_still_case_sensitive_c06():
    """C06 is a separate decision: 'Land transportation' (as in the data) still gets no site."""
    assert m.sites_for("Land Transportation", "x") == ["fine", "gogreen"]
    assert m.sites_for("Land transportation", "x") == []


@pytest.mark.parametrize("keyword", ["leather cleaner", "fabric refresher", "floor cleaner"])
def test_mumzworld_and_saco_exclusions_unchanged(keyword):
    expected = [s for s in config.TARGET_MAP["Home"]
                if not (s == "mumzworld" and keyword in config.MUMZWORLD_EXCLUSIONS)
                and not (s == "saco" and keyword in config.SACO_EXCLUSIONS)]
    assert m.sites_for("Home", keyword) == expected


@pytest.mark.parametrize("type_of_product, modifiers, expected", [
    ("H1-Floor cleaner", "", ("floor cleaner", "floor cleaner", {}, "volume")),
    ("P1-Pet wipes", "Amazon: pet wipes; extra", ("pet wipes", "pet wipes extra", {"amazon": ["pet wipes"]}, "units")),
    ("Glass cleaner", None, ("glass cleaner", "glass cleaner", {}, "volume")),
    ("C2-Microfiber - car", "saco:a;saco:b", ("microfiber - car", "microfiber - car", {"saco": ["a", "b"]}, "units")),
])
def test_parse_instruction_keeps_the_old_rules(type_of_product, modifiers, expected):
    plan = m.parse_instruction({"Type of product": type_of_product, "Generic product type": "G",
                                "Search Modifiers": modifiers})
    assert (plan["base_keyword"], plan["search_keyword"], plan["site_specific_keywords"], plan["search_mode"]) == expected


# --- NUL in a field (finding 2026-11-17: one bad title used to abort the whole run) ----------------
def test_nul_in_a_field_is_stripped_and_the_row_is_written(tmp_path):
    row = m.build_row_data({}, "Industry", "", "\x00", "Generic", "amazon", "B2B", "2026-11-14")
    assert row["type_of_product"] == "" and list(row) == config.CSV_COLUMNS
    p = tmp_path / "x.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=config.CSV_COLUMNS).writerow(row)  # must not raise


def test_a_title_with_nul_does_not_abort_the_run(paths):
    def nul_title(self, keyword, mode):
        return [{**_product(self.site, keyword, "good"), "Product": "good\x00 title", "Company": "Br\x00and"}]

    assert _main(paths, factory=_factory({"saco": nul_title})) == 0
    saco = [r for r in _rows(paths["out"]) if r["source"] == "saco"]
    assert saco and all(r["product"] == "good title" and r["company"] == "Brand" for r in saco)


def test_strip_nul_keeps_other_types():
    assert m.strip_nul("a\x00b\x00") == "ab"
    assert m.strip_nul(None) is None and m.strip_nul(12.5) == 12.5 and m.strip_nul(3) == 3
