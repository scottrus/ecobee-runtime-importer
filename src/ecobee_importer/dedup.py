"""Suppress re-writing buckets that have not changed.

Each cycle deliberately re-requests `ECOBEE_OVERLAP_MINUTES` before the
watermark, because ecobee fills in late and revises recent buckets. With a
60-minute overlap and a 15-minute interval, every bucket falls inside that
window for FOUR consecutive cycles — so without this, each one is written four
times, forever, with no restart involved.

VictoriaMetrics stores those duplicates unless deduplication is configured, and
raw-sample functions then over-count: `count_over_time` and `sum_over_time`
inflate by roughly the duplication factor. That matters because summing
`ecobee_equipment_runtime_seconds` is the duty-cycle question this project
exists to answer, and 4x of it read as 50 hours of compressor runtime in a
24-hour day.

The fix keeps the overlap — late and revised data still arrives — and drops only
the writes that would change nothing. A bucket whose VALUE has changed is still
written, which is the entire point of re-requesting it.

Restarts still re-send, because the cache is in memory. That is a deliberate
trade: persisting it would reintroduce the state-corruption class that keeping
the watermark in memory was chosen to avoid, and a restart is rare.
"""

from __future__ import annotations

import logging

from .transform import Sample

_LOGGER = logging.getLogger(__name__)

# Identity of one point: the series it belongs to, plus its bucket timestamp.
Key = tuple[str, tuple[tuple[str, str], ...], int]


def _key(sample: Sample) -> Key:
    return (sample.name, tuple(sorted(sample.labels.items())), sample.timestamp_ms)


class SentCache:
    """Remembers the value last written for each (series, bucket)."""

    def __init__(self) -> None:
        self._seen: dict[Key, float] = {}

    def __len__(self) -> int:
        return len(self._seen)

    def is_unsent(self, sample: Sample) -> bool:
        """Whether this one sample is new, or carries a changed value.

        The single-sample form exists so the import loop can stream: holding
        every sample to filter a list is what this class is trying to avoid.
        """
        return self._seen.get(_key(sample)) != sample.value

    def unsent(self, samples: list[Sample]) -> list[Sample]:
        """Return only the samples that are new, or whose value changed.

        Comparison is on the value, not merely on presence: ecobee revises
        recent buckets, and a revision must reach the database.
        """
        return [s for s in samples if self.is_unsent(s)]

    def remember(self, samples: list[Sample]) -> None:
        """Record samples as written. Call only AFTER a successful write.

        Recording before the write would silently drop data whenever the write
        failed: the retry would consider those buckets already sent.
        """
        for sample in samples:
            self._seen[_key(sample)] = sample.value

    def prune(self, cutoff_ms: int) -> int:
        """Forget buckets older than the window that can still be re-requested.

        Without this the cache grows for the life of the process. Anything older
        than the overlap will never be offered again, so remembering it buys
        nothing.
        """
        stale = [k for k in self._seen if k[2] < cutoff_ms]
        for key in stale:
            del self._seen[key]
        return len(stale)
