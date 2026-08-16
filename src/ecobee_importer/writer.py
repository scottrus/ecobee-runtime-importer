"""Writing samples to VictoriaMetrics.

Samples carry their own timestamps, so this is backfill rather than scraping.
VictoriaMetrics accepts out-of-order and historical writes without limitation
inside the retention period, and automatically resets its rollup result cache
for samples older than `-search.cacheTimestampOffset` (5m by default) — which
every sample here is. No cache flush is needed.

The format is Prometheus exposition text with an explicit millisecond timestamp
per line, accepted at `/api/v1/import/prometheus`. Chosen over remote-write
because it needs neither protobuf nor snappy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import requests

from .transform import Sample

_LOGGER = logging.getLogger(__name__)

# Lines per POST. Keeps a single request bounded when a long backfill runs.
BATCH_LINES = 10_000


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(sample: Sample, extra_labels: Mapping[str, str] | None = None) -> str:
    merged = dict(sample.labels)
    if extra_labels:
        # Generated labels win. Collisions are rejected at config load, so this
        # is belt and braces rather than the real guard.
        merged = {**extra_labels, **merged}
    labels = ",".join(
        f'{key}="{_escape(value)}"' for key, value in sorted(merged.items())
    )
    return f"{sample.name}{{{labels}}} {sample.value} {sample.timestamp_ms}"


def _batches(samples: Sequence[Sample], size: int) -> Iterable[Sequence[Sample]]:
    for start in range(0, len(samples), size):
        yield samples[start : start + size]


class VictoriaMetricsWriter:
    def __init__(
        self,
        url: str,
        timeout: int = 60,
        extra_labels: Mapping[str, str] | None = None,
        auth_header_file: str = "",
    ):
        self.url = url
        self.timeout = timeout
        self.extra_labels = dict(extra_labels or {})
        self.auth_header_file = auth_header_file
        self._session = requests.Session()

    def _headers(self) -> dict:
        headers = {"Content-Type": "text/plain"}
        if self.auth_header_file:
            # Read per write rather than cached: the file is typically a Secret
            # mount, and a rotated credential should not need a restart.
            try:
                value = Path(self.auth_header_file).read_text().strip()
            except OSError as err:
                raise RuntimeError(
                    f"cannot read ECOBEE_VM_AUTH_HEADER_FILE "
                    f"{self.auth_header_file}: {err}"
                ) from err
            if value:
                headers["Authorization"] = value
        return headers

    def write(self, samples: Sequence[Sample]) -> int:
        """POST samples, returning the number written.

        Raises on failure. The caller must not advance its watermark unless this
        returns — that is what makes a VictoriaMetrics outage lossless rather
        than a silent gap.
        """
        if not samples:
            return 0

        written = 0
        headers = self._headers()
        for batch in _batches(samples, BATCH_LINES):
            payload = "\n".join(render(s, self.extra_labels) for s in batch) + "\n"
            resp = self._session.post(
                self.url,
                data=payload.encode(),
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            written += len(batch)

        _LOGGER.info("Wrote %d samples to %s", written, self.url)
        return written
