from datetime import datetime

import pytest
from conftest import claude, context, meta, token, write
from tokmeter.pricing import Catalog
from tokmeter.query import aggregate, bucket_keys


def ms(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def test_new_models_match_exactly():
    catalog = Catalog()
    assert catalog.lookup("gpt-6-astra")["input"] == 10
    assert catalog.lookup("gpt-5.6-sol")["input"] == 4
    assert catalog.lookup("claude-opus-4-8")["input"] == 5
    assert catalog.lookup("claude-fable-5-1")["cr"] == 0.25
    assert catalog.lookup("gpt-5.99") is None
    assert catalog.lookup("claude-opus-4-99") is None


def test_unknown_model_remains_visible_as_unpriced(store):
    write(store.sources.codex / "sessions/a.jsonl", [meta(), context("future-model"), token()])
    s = store.refresh()
    r = aggregate(s, ms("2026-09-05T00:00Z"), ms("2026-09-06T00:00Z"), source="all")
    assert r["totals"]["calls"] == r["totals"]["unpricedCalls"] == 1
    assert r["byModel"][0]["name"] == "future-model" and sum(r["buckets"]["unpricedCalls"]) == 1


def test_components_sum_to_total_for_both_sources(store):
    write(store.sources.codex / "sessions/a.jsonl", [meta(), context(), token()])
    write(store.sources.claude / "p/a.jsonl", [claude(cache_read_input_tokens=10)])
    r = aggregate(store.refresh(), ms("2026-09-05T00:00Z"), ms("2026-09-06T00:00Z"), source="all")
    assert sum(x["val"] for x in r["costSplit"]) == pytest.approx(r["totals"]["cost"])
    assert sum(r["buckets"]["cost"]) == pytest.approx(r["totals"]["cost"])
    # Codex input includes cached input; reasoning is a subset of output.
    assert r["totals"]["cost"] == pytest.approx(
        (900 * 10 + 100 * 1 + 100 * 50 + 100 * 5 + 100 * 25 + 10 * 0.5) / 1e6
    )


def test_long_context_and_confirmed_fast(store):
    write(
        store.sources.codex / "sessions/a.jsonl",
        [meta(), context(), token(inp=300000, cached=100000, out=100, reason=20, service_tier="priority")],
    )
    s = store.refresh()
    assert s.frame["cost"][0] == pytest.approx((200000 * 20 + 100000 * 2 + 100 * 75) * 2 / 1e6)
    assert s.frame["long_ctx"][0] and not s.frame["tier_assumed"][0]


@pytest.mark.parametrize(
    "start,end,hours",
    [
        ("2026-03-08T05:00Z", "2026-03-09T04:00Z", 23),
        ("2026-11-01T04:00Z", "2026-11-02T05:00Z", 25),
    ],
)
def test_dst_day_boundaries(start, end, hours):
    keys = bucket_keys(ms(start), ms(end), "1h", "America/New_York")
    assert len(keys) == hours and len(set(keys)) == hours
    assert bucket_keys(ms(start), ms(end), "1d", "America/New_York") == [ms(start)]


def test_winter_day_and_non_hour_timezone(store):
    write(store.sources.claude / "p/a.jsonl", [claude(ts="2026-01-01T04:30:00Z")])
    s = store.refresh()
    r = aggregate(s, ms("2025-12-31T05:00Z"), ms("2026-01-01T05:00Z"), "1d", "ET")
    assert r["totals"]["calls"] == 1 and r["buckets"]["calls"] == [1]
    r = aggregate(s, ms("2026-01-01T04:00Z"), ms("2026-01-01T06:00Z"), "1h", "Asia/Kolkata")
    assert sum(r["buckets"]["calls"]) == 1


def test_bucket_limit_and_bad_queries(store):
    s = store.refresh()
    a = ms("2026-01-01T00:00Z")
    b = ms("2026-02-01T00:00Z")
    r = aggregate(s, a, b, "1m", "UTC")
    assert len(r["buckets"]["ts"]) <= 5000 and r["granularity"] != "1m"
    for params in [(b, a, "1h", "UTC"), (a, b, "invalid", "UTC"), (a, b, "1h", "Local")]:
        with pytest.raises(ValueError):
            aggregate(s, *params)


def test_start_inside_second_dst_fold():
    assert bucket_keys(ms("2026-11-01T06:30Z"), ms("2026-11-01T07:00Z"), "1h", "America/New_York") == [
        ms("2026-11-01T06:00Z")
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"input": 1, "output": float("nan")},
        {"input": 1, "output": 2, "long": {"threshold": 0}},
        {"input": 1, "output": 2, "long": {"threshold": 100, "output": -1}},
        {"input": 1, "output": 2, "fast_multiplier": "invalid"},
        {"output": 2},
    ],
)
def test_invalid_custom_prices_fail_before_start(tmp_path, entry):
    import json

    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"models": {"custom": entry}}))
    with pytest.raises(ValueError):
        Catalog(path)
