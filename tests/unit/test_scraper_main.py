"""Unit tests of `scraper/main.py` after F2: temp file + rename (C02), fail loudly, coverage report.

`main()` runs in-process with everything injected: a fake relevance agent (or the real
`RelevanceAgent` with a fake Bedrock client), fake scrapers (no browser), a fake browser factory
(`FAKE_BROWSER`) and a no-op sleep. All files live in `tmp_path`.

What it proves:
- success: the output CSV is replaced, no temp file is left, header + rows with `CSV_COLUMNS`;
- any failure (instructions missing, browser start, preflight, fatal AI error even when a scraper
  swallows it, unexpected exception, 0 rows, AI failure rate above 5 %) → exit 1 and the previous
  CSV is byte-for-byte untouched; the coverage report is still written;
- append mode copies the previous rows and adds no second header;
- one `scrape()` exception is not fatal: logged, counted in the coverage `error` column;
- C08: `build_row_data` keys are exactly `config.CSV_COLUMNS` (15 since F6: + seller, origin) and
  survive a DictWriter round trip; rows without Seller/Origin get '' and 'unknown';
- the TARGET_MAP lookup and the Mumzworld/SACO exclusions are unchanged (C06 is not fixed here);
  F6 adds 'noon' to Home, Automotive and Pets only;
- F6 Apify paths: an Apify preflight failure exits 1 before any scraper is built; an
  `ApifyFatalError` (incl. `ApifyBudgetExceeded`) aborts like a fatal AI error; an `ApifyRunFailed`
  is a per-keyword error, but failed / attempted Apify searches above
  `APIFY_MAX_FAILED_KEYWORD_RATE` (5 %) exit 1 with the temp file kept; append onto a file with another header (pre-F6) exits 1 and writes nothing;
  `make_scrapers` builds the Apify adapters without reading any secret.
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
# The same ai_service / apify_client module objects main.py uses (load_module would create copies).
ai = sys.modules[m.RelevanceAgent.__module__]
from services import apify_client as ac  # noqa: E402
from scrapers import amazon_apify, noon_apify  # noqa: E402

# A previous output with the current header, so append-mode failure cases reach their own failure
# (a file with another header is refused earlier, see test_append_onto_a_pre_f6_file_is_refused).
OLD_CSV = ",".join(config.CSV_COLUMNS) + "\n" + ",".join(["previous run row"] * len(config.CSV_COLUMNS)) + "\n"
PRE_F6_COLUMNS = ["date", "industry", "subindustry", "type_of_product", "generic_product_type", "product",
                  "price_sar", "company", "source", "url", "unit_of_measurement", "total_quantity", "channel"]
SITES = ("amazon", "noon", "mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense")
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
    product = {"Product": f"{tag} {site} {keyword}", "Price_SAR": 12.5, "Company": "Fake brand",
               "URL": f"https://{site}.example.invalid/{tag}", "Unit of measurement": "ml", "Total quantity": 500}
    if site in m.APIFY_SITES:  # only the Apify adapters return Seller and Origin
        product.update({"Seller": f"Seller of {site}", "Origin": "imported"})
    return product


def _factory(behaviors=None, created=None):
    behaviors = behaviors or {}

    def factory(browser, agent):
        assert browser is FAKE_BROWSER
        scrapers = {s: FakeScraper(s, None if s in m.SITES_WITHOUT_AI else agent, behaviors.get(s))
                    for s in SITES}
        if created is not None:
            created.append(scrapers)
        return scrapers
    return factory


@pytest.fixture
def paths(tmp_path):
    instr = tmp_path / "analysis.csv"
    pd.DataFrame(INSTRUCTIONS).to_csv(instr, index=False)
    return {"instr": instr, "out": tmp_path / "competitors_complete.csv", "cov": tmp_path / "cov" / "coverage.csv"}


def _write_instructions(paths, rows):
    pd.DataFrame(rows).to_csv(paths["instr"], index=False)


class FakeBrowser:
    """Stands in for `services.browser.BrowserSession`; main closes it at the end of the run."""
    closed = 0

    def close(self):
        FakeBrowser.closed += 1


FAKE_BROWSER = FakeBrowser()


def _main(paths, mode="overwrite", agent=None, factory=None, installer=None, sleeps=None,
          apify_preflight=lambda: None):
    agent = agent or FakeAgent()
    argv = ["--analysis_file", str(paths["instr"]), "--output_mode", mode, "--output_file", str(paths["out"]),
            "--coverage_file", str(paths["cov"])]
    return m.main(argv, agent_factory=lambda: agent, scraper_factory=factory or _factory(),
                  browser_factory=installer or (lambda: FAKE_BROWSER),
                  sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                  apify_preflight=apify_preflight)


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
    assert {r["source"] for r in rows} == {"amazon", "noon", "saco", "gogreen", "aerosense"}
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
    "browser": dict(installer=_boom),
    "preflight": dict(agent=FakeAgent(preflight_error=m.RelevanceAgentFatalError("fake: no access"))),
    "factory": dict(factory=_boom),
    "zero-rows": dict(factory=_factory({s: (lambda self, k, mode: []) for s in
                                        ("amazon", "noon", "mumzworld", "saco", "gogreen", "aerosense")})),
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
    """`is_relevant()` records a fatal AI error and returns False, so a scraper ends normally;
    main must look at agent.fatal_error."""
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    agent = FakeAgent()

    def swallow(self, keyword, mode):
        agent.fatal_error = m.RelevanceAgentFatalError("fake AccessDeniedException")
        return []  # what a scraper returns after catching the exception

    created = []
    assert _main(paths, agent=agent, factory=_factory({"saco": swallow}, created)) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    cov = _coverage(paths)
    assert cov[-1]["site"] == "saco" and cov[-1]["error"] == "RelevanceAgentFatalError"
    assert cov[-1]["rows_written"] == "0"
    assert created[-1]["noon"].calls == [], "nothing is scraped after the fatal error"


def test_fatal_ai_error_raised_by_a_scraper_aborts(paths):
    def raise_fatal(self, keyword, mode):
        raise m.RelevanceAgentFatalError("fake ExpiredTokenException")

    assert _main(paths, factory=_factory({"amazon": raise_fatal})) == 1
    assert not paths["out"].exists()


def test_one_site_error_is_logged_counted_and_does_not_abort(paths):
    calls = []

    def flaky(self, keyword, mode):  # the first search fails, the others work
        calls.append(keyword)
        if len(calls) == 1:
            raise TimeoutError("fake page timeout")
        return None  # default products

    extra = {"Type of product": "H2-Glass cleaner", "Sub industry": "Home", "Industry": "Home care",
             "Generic product type": "Glass cleaner", "Search Modifiers": ""}
    _write_instructions(paths, INSTRUCTIONS + [extra])
    assert _main(paths, factory=_factory({"saco": flaky})) == 0
    saco = [r for r in _coverage(paths) if r["site"] == "saco"]
    assert [r["error"] for r in saco] == ["TimeoutError", ""]
    assert "saco" in {r["source"] for r in _rows(paths["out"])}


# --- coverage -----------------------------------------------------------------------------------
def test_coverage_report_counts_found_relevant_and_rows(paths):
    assert _main(paths) == 0
    cov = _coverage(paths)
    assert list(cov[0]) == m.COVERAGE_COLUMNS
    keys = [(r["subindustry"], r["type_of_product"], r["site"], r["keyword"]) for r in cov]
    assert len(keys) == len(set(keys))
    pets = [r for r in cov if r["subindustry"] == "Pets"]
    assert [(r["site"], r["keyword"]) for r in pets] == [("amazon", "pet wipes"), ("amazon", "dog wipes"),
                                                         ("noon", "pet wipes")]
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


def test_csv_columns_are_the_13_originals_plus_seller_and_origin():
    """F6 (Mafer, 2026-11-22): seller and origin are appended; the 13 original columns keep their order."""
    assert config.CSV_COLUMNS == PRE_F6_COLUMNS + ["seller", "origin"]


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


# --- F6: Apify ------------------------------------------------------------------------------------
TEMP_NAME = "competitors_complete.tmp.csv"


def _raise(error, keywords=None):
    """Scraper behavior: raise ``error`` (for every keyword, or only for ``keywords``)."""
    def behavior(self, keyword, mode):
        if keywords is None or keyword in keywords:
            raise error
        return None  # default products
    return behavior


@pytest.mark.parametrize("error", [ac.ApifyFatalError("fake Apify POST /acts/x/runs: HTTP 401"),
                                   ac.ApifyBudgetExceeded("fake Apify budget USD 20.00 would be exceeded")])
@pytest.mark.parametrize("mode", ["overwrite", "append"])
def test_apify_fatal_error_aborts_and_keeps_the_previous_csv(paths, error, mode):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    created = []
    assert _main(paths, mode=mode, factory=_factory({"amazon": _raise(error)}, created)) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    cov = _coverage(paths)
    assert (cov[-1]["site"], cov[-1]["error"], cov[-1]["rows_written"]) == ("amazon", type(error).__name__, "0")
    # Subindustries run in groupby order (Aviation, Home, Pets): amazon is the first Home site.
    assert len(created) == 2, "Pets is never reached"
    assert all(scrapers[s].calls == [] for scrapers in created for s in ("saco", "noon")), \
        "nothing is scraped after the fatal error"


def test_apify_run_failed_is_per_keyword_but_fails_the_run(paths):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    created = []
    factory = _factory({"noon": _raise(ac.ApifyRunFailed("fake Apify run FAILED"), {"floor cleaner"})}, created)
    assert _main(paths, factory=factory) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV, "the output is not replaced"
    temp = paths["out"].parent / TEMP_NAME
    assert temp.is_file(), "the partial rows stay in the temp file"
    sources = {r["source"] for r in _rows(temp)}
    assert {"amazon", "saco", "aerosense"} <= sources, "scraping went on after the failure"
    noon_calls = [c for scrapers in created for c in scrapers["noon"].calls]
    assert [k for k, _ in noon_calls] == ["floor cleaner", "pet wipes"], "noon kept being used after the failure"
    cov = _coverage(paths)
    failed = [r for r in cov if r["error"]]
    assert [(r["site"], r["keyword"], r["error"]) for r in failed] == [("noon", "floor cleaner", "ApifyRunFailed")]


def test_apify_failed_keywords_up_to_the_limit_are_tolerated(paths, monkeypatch):
    monkeypatch.setattr(config, "APIFY_MAX_FAILED_KEYWORD_RATE", 0.5)
    warnings = []
    monkeypatch.setattr(m.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    factory = _factory({"noon": _raise(ac.ApifyRunFailed("fake Apify run FAILED"), {"floor cleaner"})})
    assert _main(paths, factory=factory) == 0
    assert "noon" in {r["source"] for r in _rows(paths["out"])}  # the Pets keyword still worked
    assert not (paths["out"].parent / TEMP_NAME).exists()
    assert any("within the 50% limit" in w and "floor cleaner" in w for w in warnings), "a tolerated failure is logged"


def test_apify_failures_over_the_rate_exit_1(paths, monkeypatch):
    monkeypatch.setattr(config, "APIFY_MAX_FAILED_KEYWORD_RATE", 0.5)
    factory = _factory({"noon": _raise(ac.ApifyRunFailed("fake")), "amazon": _raise(TimeoutError(), {"pet wipes"})})
    assert _main(paths, factory=factory) == 1
    assert not paths["out"].exists()


def test_a_browser_site_error_is_not_counted_as_an_apify_failure(paths):
    assert config.APIFY_MAX_FAILED_KEYWORD_RATE == 0.05
    extra = {"Type of product": "H2-Glass cleaner", "Sub industry": "Home", "Industry": "Home care",
             "Generic product type": "Glass cleaner", "Search Modifiers": ""}
    _write_instructions(paths, INSTRUCTIONS + [extra])
    factory = _factory({"saco": _raise(TimeoutError("fake page timeout"), {"floor cleaner"})})
    assert _main(paths, factory=factory) == 0
    cov = _coverage(paths)
    assert any(r["site"] == "saco" and r["error"] == "TimeoutError" for r in cov)
    assert m.apify_failed_keywords(cov) == []


@pytest.mark.parametrize("ok, bad, exceeded", [
    (19, 1, False),   # 1 / 20 = 5 %: not above the limit
    (19, 2, True),    # 2 / 21 > 5 %
    (0, 1, True),     # every Apify search failed
    (0, 0, False),    # no Apify search at all
    (100, 5, False),  # 5 / 105 < 5 %
])
def test_apify_failures_exceeded_uses_a_strict_rate(ok, bad, exceeded):
    entries = ([m.coverage_entry("S", "T", "amazon", f"ok{i}", {}, 1) for i in range(ok)]
               + [m.coverage_entry("S", "T", "noon", f"bad{i}", {}, 0, "ApifyRunFailed") for i in range(bad)]
               + [m.coverage_entry("S", "T", "saco", "x", {}, 0, "TimeoutError")] * 7)  # browser site: ignored
    failed, attempted, result = m.apify_failures_exceeded(entries, 0.05)
    assert (len(failed), attempted, result) == (bad, ok + bad, exceeded)


def test_apify_failed_keywords_helper():
    entries = [m.coverage_entry("S", "T", site, "k", {}, 0, error) for site, error in [
        ("amazon", ""), ("amazon", "ApifyRunFailed"), ("noon", "TimeoutError"), ("saco", "TimeoutError"),
        ("mumzworld", "ApifyRunFailed")]]
    assert [(e["site"], e["error"]) for e in m.apify_failed_keywords(entries)] == [
        ("amazon", "ApifyRunFailed"), ("noon", "TimeoutError")]
    assert m.APIFY_SITES == ("amazon", "noon")


@pytest.mark.parametrize("error", [ac.ApifyFatalError("fake Apify GET /users/me/limits: HTTP 401"),
                                   ac.ApifyRunFailed("fake Apify GET /users/me/limits: URLError")])
def test_apify_preflight_failure_exits_1_before_building_scrapers(paths, error):
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    created = []

    def preflight():
        raise error

    assert _main(paths, factory=_factory(created=created), apify_preflight=preflight) == 1
    assert created == [], "no scraper is built"
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    assert not (paths["out"].parent / TEMP_NAME).exists()


def test_apify_preflight_runs_after_the_ai_preflight(paths):
    calls = []
    agent = FakeAgent(preflight_error=m.RelevanceAgentFatalError("fake: no access"))
    assert _main(paths, agent=agent, apify_preflight=lambda: calls.append(1)) == 1
    assert calls == []
    assert _main(paths, apify_preflight=lambda: calls.append(1)) == 0
    assert calls == [1], "called once per run"


def test_main_uses_the_real_apify_preflight_by_default():
    import inspect
    assert inspect.signature(m.main).parameters["apify_preflight"].default is ac.preflight


def test_row_data_copies_seller_and_origin_or_uses_the_defaults():
    args = ("Home care", "Home", "H1-Floor cleaner", "Floor cleaner", "noon", "B2C", "2026-11-22")
    with_new = m.build_row_data({**_product("saco", "k", "good"), "Seller": "Shop\x00 N", "Origin": "local"}, *args)
    assert (with_new["seller"], with_new["origin"]) == ("Shop N", "local")
    without = m.build_row_data(_product("saco", "k", "good"), *args)
    assert (without["seller"], without["origin"]) == ("", "unknown")
    empty = m.build_row_data({**_product("saco", "k", "good"), "Seller": None, "Origin": ""}, *args)
    assert (empty["seller"], empty["origin"]) == ("", "unknown")
    assert list(with_new) == list(without) == config.CSV_COLUMNS
    assert {k: v for k, v in with_new.items() if k in PRE_F6_COLUMNS} == \
        {k: v for k, v in without.items() if k in PRE_F6_COLUMNS}


def test_browser_site_rows_get_the_default_seller_and_origin(paths):
    assert _main(paths) == 0
    for r in _rows(paths["out"]):
        if r["source"] in m.APIFY_SITES:
            assert (r["seller"], r["origin"]) == (f"Seller of {r['source']}", "imported")
        else:
            assert (r["seller"], r["origin"]) == ("", "unknown")


def test_append_onto_a_pre_f6_file_is_refused(paths):
    old = ",".join(PRE_F6_COLUMNS) + "\n" + ",".join(["x"] * len(PRE_F6_COLUMNS)) + "\n"
    paths["out"].write_text(old, encoding="utf-8")
    created = []
    assert _main(paths, mode="append", factory=_factory(created=created)) == 1
    assert paths["out"].read_text(encoding="utf-8") == old
    assert not (paths["out"].parent / TEMP_NAME).exists()
    assert created == [], "nothing is scraped"


def test_overwrite_onto_a_pre_f6_file_replaces_it(paths):
    paths["out"].write_text(",".join(PRE_F6_COLUMNS) + "\n", encoding="utf-8")
    assert _main(paths) == 0
    assert list(_rows(paths["out"])[0]) == config.CSV_COLUMNS


def test_gogreen_in_b2c_needs_no_site_keyword_and_is_told_the_channel(paths):
    # Mafer, 2026-11-25: GoGreen in Home, searched with the general keyword (its own search
    # ignores the query); B2B still needs a site-specific keyword or general modifiers.
    _write_instructions(paths, [
        INSTRUCTIONS[0],  # Home, no modifiers
        {"Type of product": "G1-Floor cleaner", "Sub industry": "Gyms", "Industry": "Sports",
         "Generic product type": "Floor cleaner", "Search Modifiers": ""},
    ])
    created = []

    def factory(browser, agent):
        scrapers = _factory(created=created)(browser, agent)
        scrapers["gogreen"].channels = []
        scrapers["gogreen"].set_channel = scrapers["gogreen"].channels.append
        return scrapers

    assert _main(paths, factory=factory) == 0
    gyms, home = created  # one set of scrapers per subindustry, in groupby order
    assert (gyms["gogreen"].channels, gyms["gogreen"].calls) == (["B2B"], [])
    assert (home["gogreen"].channels, home["gogreen"].calls) == (["B2C"], [("floor cleaner", "volume")])
    gogreen = [r for r in _rows(paths["out"]) if r["source"] == "gogreen"]
    assert gogreen and {(r["subindustry"], r["channel"]) for r in gogreen} == {("Home", "B2C")}
    assert not [r for r in _rows(paths["out"]) if r["subindustry"] == "Gyms"]


def test_target_map_noon_only_in_home_automotive_and_pets():
    assert {k for k, v in config.TARGET_MAP.items() if "noon" in v} == {"Home", "Automotive", "Pets"}
    # Mumzworld removed 2026-11-25 (HTTP 403); GoGreen added the same day (B2C, up to 4 L)
    assert config.TARGET_MAP["Home"] == ["amazon", "saco", "noon", "gogreen"]
    assert not any("mumzworld" in v for v in config.TARGET_MAP.values())
    assert config.TARGET_MAP["Automotive"] == ["amazon", "saco", "noon"]
    assert config.TARGET_MAP["Pets"] == ["amazon", "noon"]
    assert {k for k, v in config.TARGET_MAP.items() if "amazon" in v} == {"Home", "Automotive", "Pets"}


def test_apify_config_values():
    """Values decided by Mafer (2026-11-22) and docs/plan.md section 5; changing them needs her OK."""
    assert config.APIFY_AMAZON_ACTOR == "junglee~amazon-crawler"
    assert config.APIFY_NOON_ACTOR == "saswave~noon-product-scraper"
    assert config.APIFY_RUN_BUDGET_USD == 20.0
    assert config.APIFY_MAX_FAILED_KEYWORD_RATE == 0.05
    assert config.APIFY_RUN_ATTEMPTS == 2
    assert config.APIFY_MAX_RESULTS == 50
    assert 0 < config.APIFY_AMAZON_MAX_USD <= config.APIFY_RUN_BUDGET_USD
    assert 0 < config.APIFY_NOON_MAX_USD <= config.APIFY_RUN_BUDGET_USD


def test_make_scrapers_builds_the_apify_adapters_without_reading_secrets(monkeypatch):
    def no_secret(*a, **k):
        raise AssertionError("make_scrapers must not read the Apify token")

    monkeypatch.setattr(ac, "_SHARED", None)
    monkeypatch.setattr(ac, "load_token", no_secret)
    agent = FakeAgent()
    scrapers = m.make_scrapers("/x", agent)
    assert set(scrapers) == set(SITES)
    assert isinstance(scrapers["amazon"], amazon_apify.AmazonApifyScraper)
    assert isinstance(scrapers["noon"], noon_apify.NoonApifyScraper)
    assert scrapers["amazon"].relevance_agent is agent and scrapers["noon"].relevance_agent is agent
    assert ac._SHARED is None


def test_the_selenium_amazon_scraper_is_gone():
    """Apify replaces the Selenium Amazon scraper (Mafer, 2026-11-22); git history is the rollback."""
    from pathlib import Path
    assert not Path(m.SCRAPER_DIR, "scrapers", "amazon_scraper.py").exists()
    assert not hasattr(m, "AmazonScraper")


# --- Playwright browser (2026-11 migration from Selenium) -----------------------------------------
@pytest.mark.parametrize("factory", [None, _boom], ids=["success", "unexpected-error"])
def test_the_browser_is_closed_at_the_end_of_every_run(paths, factory):
    before = FakeBrowser.closed
    _main(paths, factory=factory)
    assert FakeBrowser.closed == before + 1


def test_a_browser_that_cannot_start_exits_1_before_any_scraper_is_built(paths):
    built = []
    assert _main(paths, installer=_boom, factory=_factory(created=built)) == 1
    assert built == []


def test_a_site_that_fails_every_search_exits_1_and_keeps_the_previous_csv(paths, monkeypatch):
    """Mafer, 2026-11-25: a broken site stops the run (the pipeline then sends the failure email)."""
    errors = []
    monkeypatch.setattr(m.logger, "error", lambda msg, *a, **k: errors.append(msg))
    paths["out"].write_text(OLD_CSV, encoding="utf-8")
    from services.browser import HttpStatusError
    blocked = HttpStatusError(403, "https://example.invalid/")
    assert _main(paths, factory=_factory({"saco": _raise(blocked)})) == 1
    assert paths["out"].read_text(encoding="utf-8") == OLD_CSV
    saco = [r for r in _rows(paths["cov"]) if r["site"] == "saco"]
    assert saco and all(r["error"] == type(blocked).__name__ and r["rows_written"] == "0" for r in saco)
    assert any("Sites that failed every search: 'saco'" in e for e in errors)
    assert any("Site 'saco' failed every search" in e for e in errors)
    assert not any("'amazon'" in e for e in errors)


def test_a_site_failing_its_first_searches_stops_the_run_at_once(paths):
    rows = [{"Type of product": f"H{i}-Cleaner {i}", "Sub industry": "Home", "Industry": "Home care",
             "Generic product type": "Cleaner", "Search Modifiers": ""} for i in range(6)]
    _write_instructions(paths, rows)
    from services.browser import HttpStatusError
    created = []
    assert _main(paths, factory=_factory({"saco": _raise(HttpStatusError(403, "u"))}, created)) == 1
    assert not paths["out"].exists()
    saco = [r for r in _coverage(paths) if r["site"] == "saco"]
    assert len(saco) == m.FAILED_SEARCHES_TO_ABORT == 3, "nothing is searched after the third failure"


def test_site_failing_from_the_start():
    entry = m.coverage_entry
    failed = [entry("S", "T", "mumzworld", k, {}, 0, "HttpStatusError") for k in "abc"]
    assert "site 'mumzworld' failed its first 3 searches (HttpStatusError)" in \
        m.site_failing_from_the_start(failed, "mumzworld")
    assert m.site_failing_from_the_start(failed[:2], "mumzworld") == ""
    assert m.site_failing_from_the_start([entry("S", "T", "mumzworld", "z", {}, 1)] + failed, "mumzworld") == ""
    assert m.site_failing_from_the_start(failed, "saco") == ""


def test_sites_failing_every_search():
    entry = m.coverage_entry
    entries = [entry("S", "T", "gogreen", "a", {}, 0, "LanguageUnavailable"),
               entry("S", "T", "gogreen", "b", {}, 0, "LanguageUnavailable"),
               entry("S", "T", "fine", "a", {}, 0, "HttpStatusError"),
               entry("S", "T", "fine", "b", {}, 2),
               entry("S", "T", "saco", "a", {}, 0)]
    assert m.sites_failing_every_search(entries) == {"gogreen": 2}
    assert m.sites_failing_every_search([]) == {}
