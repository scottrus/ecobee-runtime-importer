"""Tests for duplicate-write suppression.

The overlap window offers every bucket four times (60-minute overlap, 15-minute
interval). Writing all four inflates count_over_time and sum_over_time over the
imported series — which read as 50 hours of compressor runtime in a 24-hour day
before this existed.
"""

from ecobee_importer.dedup import SentCache
from ecobee_importer.transform import Sample


def sample(value: float, ts: int = 1_000, thermostat: str = "Basement") -> Sample:
    return Sample(
        "ecobee_zone_temperature_fahrenheit", {"thermostat": thermostat}, value, ts
    )


def test_first_sight_is_sent():
    assert SentCache().unsent([sample(72.1)]) == [sample(72.1)]


def test_identical_repeat_is_suppressed():
    """The steady-state case: the same bucket offered again, unchanged."""
    cache = SentCache()
    first = [sample(72.1)]
    cache.remember(cache.unsent(first))

    assert cache.unsent([sample(72.1)]) == []


def test_a_revised_value_is_sent_again():
    """ecobee revises recent buckets — that is why the overlap exists at all.

    Suppressing on presence rather than value would silently discard the
    correction and leave the wrong number in the database forever.
    """
    cache = SentCache()
    cache.remember([sample(72.1)])

    assert cache.unsent([sample(73.4)]) == [sample(73.4)]


def test_same_value_at_a_different_bucket_is_sent():
    """Identity is (series, timestamp), not value — a flat temperature must
    still produce a sample in every bucket."""
    cache = SentCache()
    cache.remember([sample(72.1, ts=1_000)])

    assert cache.unsent([sample(72.1, ts=2_000)]) == [sample(72.1, ts=2_000)]


def test_series_are_distinguished_by_labels():
    cache = SentCache()
    cache.remember([sample(72.1, thermostat="Basement")])

    fresh = cache.unsent([sample(72.1, thermostat="Upstairs")])
    assert [s.labels["thermostat"] for s in fresh] == ["Upstairs"]


def test_prune_drops_buckets_that_can_no_longer_be_offered():
    cache = SentCache()
    cache.remember([sample(1.0, ts=1_000), sample(2.0, ts=5_000)])

    assert cache.prune(3_000) == 1
    assert len(cache) == 1
    # The pruned bucket is no longer suppressed, but it is also outside the
    # window the importer will ever request again.
    assert cache.unsent([sample(1.0, ts=1_000)]) != []


def test_nothing_is_remembered_until_it_is_written():
    """remember() is called only after a successful write.

    If a write fails the samples must be offered again on the next cycle, not
    treated as already sent.
    """
    cache = SentCache()
    fresh = cache.unsent([sample(72.1)])
    # write fails here, so remember() is never reached
    assert cache.unsent(fresh) == fresh
