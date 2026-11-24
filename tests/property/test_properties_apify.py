"""Property tests of the Apify adapters and client (F6). No network: fakes only.

- The adapters' own parsers never raise and return None or a valid value for any text:
  `parse_volume` -> ("ml" | "fl oz", finite > 0), `parse_count` -> int >= 1, `parse_price` ->
  finite > 0.
- C10: "n x v ml" is n*v ml (the pack multiplies), while a dimension ("a x b cm") anywhere in
  the text never changes `parse_volume` or `parse_count`; every row `AmazonApifyScraper.scrape`
  returns has a finite quantity > 0, a price > 0 and a unit in {ml, fl oz, units}.
- `derive_origin` always returns local / imported / unknown, and imported only from a country of
  origin; an ambiguous title (count, unread pack, bundle, dose, capacity) never gives a size.
- C09: Wayakit (any casing or spacing) in brand, seller or title is always dropped with reason
  "wayakit" by both converters; no kept candidate has a Wayakit Company or Seller.
- Noon makes exactly one actor run per keyword and `shipping_banner_details` never decides origin.
- Budget: for any sequence of run costs, `spent_usd` never exceeds the budget and
  `ApifyBudgetExceeded` is raised exactly when spent + next cap > budget, before any request.
- No token in the text of any exception the client raises, including a token with whitespace or
  control characters (refused at construction).
"""
from __future__ import annotations

import math
import random
import sys
import urllib.error

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from tests.conftest import load_module
from tests.unit._fake_apify import FakeApify, FakeClient, FakeClock, Http, Net, script_run
from tests.unit._fake_bedrock import FakeBedrock

pytestmark = pytest.mark.property

m = load_module("scraper/main.py")
from scrapers import amazon_apify, noon_apify  # noqa: E402
from scrapers import apify_common as common  # noqa: E402
from services import apify_client as ac  # noqa: E402

ai = sys.modules[m.RelevanceAgent.__module__]
any_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)
UNIT_WORDS = ["ml", "mL", "Milliliters", "L", "litre", "Liters", "ltr", "fl oz", "FL. OZ.", "Fluid Ounces",
              "oz", "g", "Grams", "kg", "lbs", "wipes", "count", "pcs", "Pack", "x", "×", ""]
number = st.one_of(st.integers(0, 100000).map(str),
                   st.floats(0, 10000, allow_nan=False).map(lambda f: f"{f:.2f}"),
                   st.sampled_from(["1,000", ".5", "1e3", "٤٥", "0", "12,5"]))
sizeish = st.lists(st.one_of(number, st.sampled_from(UNIT_WORDS), st.sampled_from(["Pack of", "Set of", "3X"])),
                   max_size=6).map(" ".join)
texty = st.one_of(any_text, sizeish)


# --- parsers never raise ----------------------------------------------------------------------------
@given(texty)
@example("9" * 5000 + " ml")
@example("1e3 ml")
@example("0 ml")
def test_parse_volume_is_none_or_valid(text):
    result = common.parse_volume(text)
    if result is not None:
        unit, amount = result
        assert unit in ("ml", "fl oz")
        assert math.isfinite(amount) and amount > 0


@given(texty)
@example("0 wipes")
@example("9999 x 9999 wipes")
def test_parse_count_is_none_or_a_positive_int(text):
    result = common.parse_count(text)
    assert result is None or (isinstance(result, int) and not isinstance(result, bool) and 1 <= result <= 100000)


@given(st.one_of(texty, st.none(), st.integers(-10**9, 10**9), st.floats(allow_nan=True, allow_infinity=True)))
@example(0.00390625)  # found by Hypothesis: rounds to 0.0, which is not a price > 0
@example("0.001")
def test_parse_price_is_none_or_finite_positive(value):
    result = common.parse_price(value)
    assert result is None or (math.isfinite(result) and result > 0)


@given(st.integers(1, 100), st.integers(1, 5000), st.sampled_from(["{n} x {v} ml", "{n}x {v}ml", "{n} × {v} ml"]))
def test_pack_multiplies_c10(n, v, fmt):
    unit, amount = common.parse_volume(fmt.format(n=n, v=v))
    assert unit == "ml" and amount == pytest.approx(n * v)


SIZED_TEMPLATES = ["Cleaner {v} ml", "Cleaner {v} ml, Pack of {k}", "{k} x {v} ml refill", "Shampoo {v} L",
                   "Cloths {n} pcs", "Pack of {k}, {n} wipes", "Brushes {k} Pack", "Towel {n} count"]
dimension = st.builds(lambda a, b, unit, sep: f"{a}{sep}x{sep}{b}{unit}", st.integers(1, 300), st.integers(1, 300),
                      st.sampled_from([" cm", "cm", " mm", " inch", " in", '"', " m"]), st.sampled_from(["", " "]))


@given(st.sampled_from(SIZED_TEMPLATES), st.integers(1, 2000), st.integers(2, 12), st.integers(1, 500), dimension,
       st.booleans())
@example("Cloths {n} pcs", 1, 2, 8, "40 x 40 cm", False)
@example("Cleaner {v} ml", 500, 2, 1, "25 x 8 cm", False)
def test_a_dimension_never_changes_size_or_count(template, v, k, n, dim, before):
    text = template.format(v=v, k=k, n=n)
    with_dim = f"{dim}, {text}" if before else f"{text}, {dim}"
    assert common.parse_volume(with_dim) == common.parse_volume(text)
    assert common.parse_count(with_dim) == common.parse_count(text)


@given(st.integers(1, 2000), dimension)
def test_a_dimension_alone_is_never_a_pack_or_a_count(v, dim):
    assert common.pack_count(dim) == 1
    assert common.parse_count(f"Microfiber cloth {dim}") is None
    assert common.parse_volume(f"Bottle {v} ml {dim}") == ("ml", float(v))


@given(st.one_of(st.none(), any_text), st.lists(any_text, max_size=3))
def test_derive_origin_is_one_of_three(country, texts):
    origin = common.derive_origin(country_of_origin=country, texts=tuple(texts))
    assert origin in {"local", "imported", "unknown"}
    if not (country or "").strip():
        assert origin != "imported", "only a country of origin can say imported (2026-11-23)"


# --- conservative mode (Mafer, 2026-11-23): an ambiguous title never gives a size --------------------
ambiguity = st.sampled_from([", 4 Count", ", 6/Carton", ", Case of 6", " Duo", " + 3 Refills",
                             " with 2 Wash Mitts", ", 1 oz per Gallon", ", makes 100 L", ", 1 oz/gallon",
                             ", Treats up to 50 Gallons", " for 55 Gallon Tanks", ", Mix 1 oz with water",
                             " Kit", " Bundle", " and 3 Pack Microfiber Towels", " and 7 L Refill"])


@given(any_text)
def test_title_volume_is_parse_volume_or_none(text):
    result = common.title_volume(text)
    assert result is None or result == common.parse_volume(text)


@given(st.integers(1, 5000), st.sampled_from(["ml", "L", "fl oz", "oz", "Gallon"]), ambiguity, st.booleans())
def test_an_ambiguity_signal_removes_the_title_size(v, unit, signal, before):
    title = f"Cleaner{signal} {v} {unit}" if before else f"Cleaner {v} {unit}{signal}"
    assert common.title_volume(title) is None
    assert common.title_volume(title, explicit_only=True) is None


@given(any_text)
def test_an_explicit_only_title_size_is_never_a_plain_oz_or_a_gallon(text):
    """With nothing to confirm it (Noon), a title size comes from ml, liters or fl oz only."""
    result = common.title_volume(text, explicit_only=True)
    if result is not None:
        kinds = [t[0] for t in common._size_tokens(text)]
        assert common.EXPLICIT in kinds and kinds[0] != common.UNUSABLE


@given(st.integers(1, 5000), st.sampled_from(["oz", "Ounces", "Gallon", "gal"]))
def test_noon_never_sizes_a_plain_oz_or_gallon_title(v, unit):
    candidate, _ = noon_apify.convert_item(_noon(name=f"good car wax {v} {unit}"), "volume")
    assert candidate["Total quantity"] is None


# --- C09: Wayakit is never a competitor -------------------------------------------------------------
wayakit = st.builds(
    lambda seps, caps, pre, post: pre + "".join(c.upper() if up else c for c, up in zip("waya" + seps + "kit", caps)) + post,
    st.sampled_from(["", " ", "-", " - ", "  "]),
    st.lists(st.booleans(), min_size=12, max_size=12),
    st.sampled_from(["", "Brand ", "x"]),
    st.sampled_from(["", " Official", " Store", "®"]),
) | st.just("وايا كيت")
field_name = st.sampled_from(["brand", "seller", "title"])


def _amazon(title="Glass cleaner 500ml", brand="Dettol", seller="Fake Shop"):
    return {"loadedCountryCode": "SA", "title": title, "asin": "B0FAKE", "brand": brand,
            "price": {"value": 20.0, "currency": "SAR"}, "seller": {"name": seller},
            "attributes": [{"key": "Item Volume", "value": "500 Milliliters"}]}


def _noon(name="Car wax 500 ml", brand="Meguiars", store="Fake Store"):
    return {"name": name, "sku": "N1", "url": "car-wax", "price": 30.0, "brand": brand, "store_name": store}


@given(wayakit, field_name, st.sampled_from(["volume", "units"]))
@example("WAYAKIT", "brand", "volume")
def test_wayakit_is_always_dropped(mark, where, mode):
    amazon = _amazon(**{where: (f"{mark} glass cleaner 500ml" if where == "title" else mark)})
    assert amazon_apify.convert_item(amazon, mode) == (None, "wayakit")
    noon_field = {"brand": "brand", "seller": "store", "title": "name"}[where]
    noon = _noon(**{noon_field: (f"{mark} car wax 500 ml" if where == "title" else mark)})
    assert noon_apify.convert_item(noon, mode) == (None, "wayakit")


@given(st.one_of(wayakit, any_text, st.none()), st.one_of(wayakit, any_text, st.none()), any_text)
def test_no_kept_candidate_is_wayakit(brand, seller, title):
    for candidate, _ in (amazon_apify.convert_item(_amazon(title=title, brand=brand, seller=seller), "volume"),
                         noon_apify.convert_item(_noon(name=title, brand=brand, store=seller), "volume")):
        if candidate is not None:
            assert not common.WAYAKIT_RE.search(str(candidate.get("Company") or ""))
            assert not common.WAYAKIT_RE.search(str(candidate.get("Seller") or ""))
            assert not common.WAYAKIT_RE.search(candidate["Product"])


@given(st.one_of(st.none(), st.booleans(), any_text, st.dictionaries(any_text, st.booleans(), max_size=3)),
       st.sampled_from(["good car wax 500 ml", "good car wax 16 fl oz", "good car wax made in KSA 1 L"]))
@example({"global": True}, "good car wax 500 ml")
def test_noon_makes_one_run_and_the_shipping_banner_never_decides_origin(banner, name):
    """2026-11-23: `shipping_banner_details` is not an origin signal and there is no details run."""
    search = [dict(_noon(name=name), shipping_banner_details=banner)]
    relevance = ai.RelevanceAgent(client=FakeBedrock(), sleep=lambda s: None, rng=random.Random(0))
    client = FakeClient(search)
    rows = noon_apify.NoonApifyScraper(None, relevance, client=client).scrape("car wax", "volume")
    assert len(client.calls) == 1
    expected = {"good car wax 500 ml": "unknown", "good car wax 16 fl oz": "unknown",
                "good car wax made in KSA 1 L": "local"}[name]
    assert [r["Origin"] for r in rows] == [expected]


# --- C10: every Amazon row is usable by the ML ------------------------------------------------------
attr_value = st.one_of(sizeish, st.none(), st.sampled_from(["500 Milliliters", "1 Liter", "16.9 Fluid Ounces",
                                                            "907 Grams", "3 Count", "2"]))
price_value = st.one_of(st.none(), st.integers(-5, 500), st.floats(allow_nan=True, allow_infinity=True),
                        st.sampled_from(["1,234.50", "0", "abc", "٤٥"]))
amazon_item = st.builds(
    lambda i, title, price, currency, cc, sponsored, vol, size, count, items: {
        "loadedCountryCode": cc, "title": title, "asin": f"B{i:05d}", "brand": "Brand",
        "price": {"value": price, "currency": currency}, "categoryPageData": {"isSponsored": sponsored},
        "attributes": [{"key": "Item Volume", "value": vol}, {"key": "Size", "value": size},
                       {"key": "Unit Count", "value": count}, {"key": "Number of Items", "value": items}]},
    st.integers(0, 30), sizeish.map(lambda s: "good " + s), price_value, st.sampled_from(["SAR", "ر.س", "USD", None]),
    st.sampled_from(["SA", "SA", "AE", None]), st.booleans(), attr_value, attr_value, attr_value, attr_value)


@given(st.lists(amazon_item, max_size=8), st.sampled_from([("glass cleaner", "volume"), ("wet wipes", "units"),
                                                           ("toilet brush", "units")]))
def test_every_amazon_row_has_a_positive_finite_size_and_price(items, query):
    keyword, mode = query
    agent = ai.RelevanceAgent(client=FakeBedrock(), sleep=lambda s: None, rng=random.Random(0))
    rows = amazon_apify.AmazonApifyScraper(None, agent, client=FakeClient(items)).scrape(keyword, mode)
    for row in rows:
        assert set(row) == {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity",
                            "Seller", "Origin"}
        qty = float(row["Total quantity"])
        assert math.isfinite(qty) and qty > 0
        assert math.isfinite(row["Price_SAR"]) and row["Price_SAR"] > 0
        assert row["Unit of measurement"] in {"ml", "fl oz", "units"}
        assert row["Origin"] in {"local", "imported", "unknown"}
        assert "good" in row["Product"]
    assert len({r["URL"] for r in rows}) == len(rows), "duplicates are dropped"


# --- budget -----------------------------------------------------------------------------------------
# Costs, caps and budgets are multiples of 1/8 (exact in binary floating point), so the comparison
# against the budget has no rounding ambiguity at the boundary.
eighths = st.integers(0, 16).map(lambda k: k / 8)


@given(st.integers(1, 8).map(lambda k: k / 8), st.integers(1, 40).map(lambda k: k / 8), st.lists(eighths, max_size=12))
def test_spend_never_exceeds_the_budget(cap, budget, raw_costs):
    costs = [min(c, cap) for c in raw_costs]
    fake = FakeApify()
    client = ac.ApifyClient("fake-token-for-budget", budget_usd=budget, urlopen=fake, sleep=lambda s: None,
                            clock=FakeClock(), run_attempts=1)
    spent = 0.0
    for n, cost in enumerate(costs):
        before = len(fake.requests)
        if spent + cap > budget:
            with pytest.raises(ac.ApifyBudgetExceeded):
                client.run_actor("a~b", {}, max_items=1, max_usd=cap, timeout_s=60)
            assert len(fake.requests) == before
            break
        script_run(fake, actor="a~b", run_id=f"r{n}", dataset_id=f"d{n}", usd=cost, polls=())
        assert client.run_actor("a~b", {}, max_items=1, max_usd=cap, timeout_s=60) == []
        spent += cost
        assert client.spent_usd == pytest.approx(spent)
        assert client.spent_usd <= budget + 1e-9


# --- no token in exceptions -------------------------------------------------------------------------
token = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-", min_size=12,
                max_size=40)
failure = st.one_of(
    st.sampled_from([400, 401, 402, 403, 404, 409, 429, 500, 502, 503, 504]).map(lambda c: ("http", c)),
    st.just(("url", None)), st.just(("timeout", None)))


@given(token, st.lists(failure, min_size=3, max_size=3), st.booleans())
def test_no_token_in_any_exception(tok, failures, during_run):
    assume(tok not in "https://api.apify.com/v2/users/me/limits/acts/a~b/runs")
    fake = FakeApify(token=tok)
    answers = []
    for kind, code in failures:
        if kind == "http":
            answers.append(Http(code, {"error": {"type": "fake-type", "message": f"token {tok} rejected"}}))
        elif kind == "url":
            answers.append(Net(urllib.error.URLError(f"failed for {tok}")))
        else:
            answers.append(Net(TimeoutError(f"timed out {tok}")))
    client = ac.ApifyClient(tok, budget_usd=5.0, urlopen=fake, sleep=lambda s: None, clock=FakeClock(),
                            run_attempts=1)
    if during_run:
        fake.add("POST", "/acts/a~b/runs", *answers)
        call = lambda: client.run_actor("a~b", {}, max_items=1, max_usd=0.5, timeout_s=60)  # noqa: E731
    else:
        fake.add("GET", "/users/me/limits", *answers)
        call = client.check_token
    with pytest.raises(ac.ApifyError) as info:
        call()
    error = info.value
    assert tok not in str(error) and tok not in repr(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert tok not in repr(client)


@given(token, st.sampled_from([" ", "\t", "\n", "\r", "\x00", "\x0b", "\x1f", "\x7f", "\u2028"]),
       st.integers(1, 11))
def test_a_token_with_whitespace_or_control_chars_is_refused_without_the_value(tok, bad, at):
    broken = tok[:at] + bad + tok[at:]
    with pytest.raises(ac.ApifyFatalError) as info:
        ac.ApifyClient(broken, budget_usd=1.0, urlopen=FakeApify(token=tok))
    text = str(info.value) + repr(info.value)
    assert tok not in text and broken not in text
    assert str(info.value) == "invalid Apify token format (key APIFY_API_TOKEN)", "a fixed message, no value"
