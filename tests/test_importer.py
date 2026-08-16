"""Tests for the import cycle's contract.

The property worth protecting: the watermark advances only after a successful
write. That is what makes a VictoriaMetrics outage a retry rather than a silent
hole in the data.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ecobee_importer.__main__ import Importer
from ecobee_importer.config import Config
from ecobee_importer.ecobee import Thermostat

IDENT = "411111111111"


def report_at(moments):
    """A minimal runtimeReport whose rows sit at the given UTC datetimes.

    Rows are emitted in thermostat-local time, as the real API does, so this
    also exercises the timezone conversion end to end.
    """
    local = [m.astimezone(timezone(timedelta(hours=-4))) for m in moments]
    return {
        "columns": "zoneAveTemp",
        "reportList": [
            {
                "thermostatIdentifier": IDENT,
                "rowList": [f"{m:%Y-%m-%d},{m:%H:%M:%S},721" for m in local],
            }
        ],
    }


class FakeClient:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def thermostats(self):
        return [Thermostat(IDENT, "Basement", "America/New_York")]

    def runtime_report(self, *args, **kwargs):
        self.calls += 1
        return self.report


class FakeWriter:
    def __init__(self, fail=False):
        self.fail = fail
        self.samples = []

    def write(self, samples):
        if self.fail:
            raise RuntimeError("VictoriaMetrics unreachable")
        self.samples.extend(samples)
        return len(samples)


def build(report, fail=False):
    cfg = Config()
    client = FakeClient(report)
    writer = FakeWriter(fail=fail)
    return Importer(cfg, client=client, writer=writer), writer


def test_cycle_writes_samples_and_advances_watermark():
    recent = datetime.now(UTC) - timedelta(minutes=30)
    importer, writer = build(report_at([recent]))
    before = importer.watermark

    written = importer.cycle()

    assert written == 1
    assert len(writer.samples) == 1
    assert importer.watermark > before


def test_watermark_does_not_advance_when_the_write_fails():
    recent = datetime.now(UTC) - timedelta(minutes=30)
    importer, _ = build(report_at([recent]), fail=True)
    before = importer.watermark

    with pytest.raises(RuntimeError):
        importer.cycle()

    assert importer.watermark == before


def test_rows_outside_the_window_are_discarded():
    """The request is padded by a day on each side; the filter is what trims it."""
    stale = datetime.now(UTC) - timedelta(days=10)
    future = datetime.now(UTC) + timedelta(hours=6)
    importer, writer = build(report_at([stale, future]))

    written = importer.cycle()

    assert written == 0
    assert writer.samples == []


def test_backfill_does_not_move_the_live_watermark():
    """An operator recovering an old gap must not rewind steady-state progress."""
    old = datetime.now(UTC) - timedelta(days=5)
    importer, writer = build(report_at([old]))
    before = importer.watermark

    written = importer.cycle(since=old - timedelta(hours=1))

    assert written == 1
    assert importer.watermark == before
