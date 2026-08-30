"""The process metrics registry (spec §5.8).

The two properties worth pinning down are the ones that would bite in production rather than in a
test: labels must not silently split one series into two, and an unbounded label must not be able to
grow the registry without limit.
"""

from __future__ import annotations

from src.shared.observability import MetricsRegistry
from src.shared.observability.metrics import MAX_SERIES


def test_counters_accumulate_per_label_set() -> None:
    registry = MetricsRegistry()

    registry.increment("requests", route="/health", status=200)
    registry.increment("requests", route="/health", status=200)
    registry.increment("requests", route="/health", status=500)

    assert registry.counter("requests", route="/health", status=200) == 2
    assert registry.counter("requests", route="/health", status=500) == 1


def test_label_order_does_not_create_a_second_series() -> None:
    """Labels are keyword arguments, so the same series can be written in either order."""
    registry = MetricsRegistry()

    registry.increment("requests", route="/health", status=200)
    registry.increment("requests", status=200, route="/health")

    assert registry.counter("requests", route="/health", status=200) == 2
    assert len(registry.snapshot()["counters"]) == 1


def test_a_status_is_the_same_series_whether_it_arrives_as_an_int_or_a_string() -> None:
    registry = MetricsRegistry()

    registry.increment("requests", status=200)
    registry.increment("requests", status="200")

    assert registry.counter("requests", status=200) == 2


def test_timings_record_count_mean_and_worst_case() -> None:
    registry = MetricsRegistry()

    registry.observe("duration", 10.0, provider="gemini")
    registry.observe("duration", 30.0, provider="gemini")

    timing = registry.timing("duration", provider="gemini")
    assert timing is not None
    assert timing.count == 2
    assert timing.mean_ms == 20.0
    assert timing.max_ms == 30.0


def test_a_timed_block_is_recorded_even_when_it_raises() -> None:
    """A provider call that fails slowly is the one most worth timing."""
    registry = MetricsRegistry()

    try:
        with registry.timed("duration", provider="gemini"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    timing = registry.timing("duration", provider="gemini")
    assert timing is not None and timing.count == 1


def test_the_cardinality_cap_drops_new_series_rather_than_growing() -> None:
    """Past the cap, a metric stops recording instead of taking the process down with it."""
    registry = MetricsRegistry()
    for index in range(MAX_SERIES):
        registry.increment("seen", label=index)

    registry.increment("new_series", label="never-seen")

    assert registry.counter("new_series", label="never-seen") == 0
    assert registry.snapshot()["seriesDropped"] == 1


def test_the_snapshot_is_serialisable_and_sorted() -> None:
    registry = MetricsRegistry()
    registry.increment("b_counter", route="/two")
    registry.increment("a_counter", route="/one")
    registry.observe("a_timing", 5.0, route="/one")

    snapshot = registry.snapshot()

    assert [counter["name"] for counter in snapshot["counters"]] == ["a_counter", "b_counter"]
    assert snapshot["counters"][0]["labels"] == {"route": "/one"}
    assert snapshot["timings"][0] == {
        "name": "a_timing",
        "labels": {"route": "/one"},
        "count": 1,
        "meanMs": 5.0,
        "maxMs": 5.0,
    }
    assert snapshot["uptimeSeconds"] >= 0
