"""Property tests of the Bedrock relevance agent (`scraper/services/ai_service.py`), fake client only.

- `clean_title`: idempotent, one line, equal to `' '.join(str(t).split())`.
- `chunked`: concatenation gives the input back; all chunks full except the last.
- `parse_items`: round trip for any permutation of valid items; a missing, duplicated or
  out-of-range index always raises `InvalidModelResponse`.
- `backoff_delay`: between half and the full capped exponential value, never above the cap.
- Agent: answers keep length and order and match the fake model; converse calls on the happy path
  == ceil(unique titles / batch size); asking again makes 0 calls; `failure_rate` in [0, 1] and
  `check_failure_rate` raises iff failures / total > 5 %.
"""
from __future__ import annotations

import json
import math
import random

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from tests.conftest import load_module
from tests.unit._fake_bedrock import FakeBedrock, oracle_relevant, titles_in

pytestmark = pytest.mark.property

ai = load_module("scraper/services/ai_service.py")

any_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)


@given(st.one_of(st.none(), any_text, st.integers(), st.floats(allow_nan=False)))
@example("a\n1. fake entry\r\nb")
@example(" x\x0by\x1c")
def test_clean_title_is_one_line_idempotent_and_matches_the_reference(title):
    cleaned = ai.clean_title(title)
    assert cleaned == ("" if title is None else " ".join(str(title).split()))
    assert ai.clean_title(cleaned) == cleaned
    assert "\n" not in cleaned and "\r" not in cleaned
    assert len(cleaned.splitlines()) <= 1


@given(st.lists(st.integers()), st.integers(min_value=1, max_value=30))
def test_chunked_round_trip(items, size):
    chunks = ai.chunked(items, size)
    assert [x for c in chunks for x in c] == items
    assert all(1 <= len(c) <= size for c in chunks)
    assert all(len(c) == size for c in chunks[:-1])
    assert len(chunks) == math.ceil(len(items) / size)


@given(st.integers(max_value=0))
def test_chunked_rejects_size_below_1(size):
    with pytest.raises(ValueError):
        ai.chunked([1], size)


@given(st.data(), st.integers(min_value=1, max_value=40), st.sampled_from(["relevant", "units"]))
def test_parse_items_round_trip(data, n, field):
    value = st.booleans() if field == "relevant" else st.integers(min_value=0, max_value=10**6)
    values = data.draw(st.lists(value, min_size=n, max_size=n))
    order = data.draw(st.permutations(range(n)))
    text = json.dumps({"items": [{"i": i, field: values[i]} for i in order]})
    assert ai.parse_items(text, n, field) == values


@given(st.data(), st.integers(min_value=1, max_value=30), st.sampled_from(["drop", "duplicate", "out_of_range"]))
def test_parse_items_rejects_bad_indexes(data, n, mutation):
    items = [{"i": i, "relevant": True} for i in range(n)]
    k = data.draw(st.integers(0, n - 1))
    if mutation == "drop":
        del items[k]
    elif mutation == "duplicate":
        items.append(dict(items[k]))
    else:
        items[k] = {"i": data.draw(st.one_of(st.integers(max_value=-1), st.integers(min_value=n))), "relevant": True}
    with pytest.raises(ai.InvalidModelResponse):
        ai.parse_items(json.dumps({"items": items}), n, "relevant")


@given(st.integers(min_value=0, max_value=50), st.floats(min_value=-2, max_value=3, allow_nan=False))
def test_backoff_delay_bounds(attempt, jitter):
    ceiling = min(ai.BACKOFF_MAX_S, ai.BACKOFF_BASE_S * 2 ** attempt)
    d = ai.backoff_delay(attempt, jitter=jitter)
    assert 0.5 * ceiling <= d <= ceiling <= ai.BACKOFF_MAX_S


# --- agent with the fake client -----------------------------------------------------------------
title = st.sampled_from(["good glass cleaner", "car shampoo", "Good  floor\ncleaner", "wipes 80", "", "x"]) | any_text


@given(st.lists(title, max_size=70), st.integers(min_value=1, max_value=30), any_text)
def test_answers_keep_order_and_calls_are_ceil_unique_over_batch(titles, batch_size, query):
    client = FakeBedrock()
    agent = ai.RelevanceAgent(client=client, batch_size=batch_size, sleep=lambda s: None, rng=random.Random(0))
    expected = [oracle_relevant(ai.clean_title(t)) for t in titles]
    assert agent.filter_relevant(titles, query) == expected
    unique = len({ai.clean_title(t) for t in titles})
    assert len(client.calls) == math.ceil(unique / batch_size)
    assert all(len(titles_in(c)) <= batch_size for c in client.calls)
    assert all("toolConfig" not in c and c["inferenceConfig"]["temperature"] == 0 for c in client.calls)
    s = agent.stats()
    assert (s["evaluated"], s["relevant"], s["failures"]) == (len(titles), sum(expected), 0)
    # Asking again: all from the cache.
    assert agent.filter_relevant(titles, query) == expected
    assert len(client.calls) == math.ceil(unique / batch_size)


@given(st.lists(st.booleans(), min_size=0, max_size=60), st.lists(st.booleans(), max_size=20))
@example([True] + [False] * 19, [])   # exactly 5 %: no raise
@example([True] * 2 + [False] * 19, [])
def test_failure_rate_and_threshold(relevance_fails, wipes_fails):
    """Each title fails on both models iff its flag is True (one call per title)."""
    def plan(req):
        return "invalid" if titles_in(req)[0].startswith("fail") else "ok"

    agent = ai.RelevanceAgent(client=FakeBedrock({ai.DEFAULT_PRIMARY_MODEL: plan, ai.DEFAULT_FALLBACK_MODEL: plan}),
                              sleep=lambda s: None, rng=random.Random(0))
    for i, fails in enumerate(relevance_fails):
        agent.is_relevant(f"{'fail' if fails else 'ok'} {i}", "q")
    for i, fails in enumerate(wipes_fails):
        agent.extract_wipes_units(f"{'fail' if fails else 'ok'} w{i}")
    failures, total = sum(relevance_fails) + sum(wipes_fails), len(relevance_fails) + len(wipes_fails)
    rate = agent.failure_rate()
    assert 0.0 <= rate <= 1.0
    assert rate == (failures / total if total else 0.0)
    if total and failures / total > 0.05:
        with pytest.raises(ai.AIFailureRateExceeded):
            agent.check_failure_rate()
    else:
        agent.check_failure_rate()
