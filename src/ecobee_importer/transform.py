"""Turn a runtimeReport response into timestamped samples.

Row timestamps are in the **thermostat's local time**, not UTC — the one
property of this response that silently corrupts data. See ARCHITECTURE.md §3.2.

VERIFIED UNITS: runtimeReport returns temperatures as **decimal degrees
Fahrenheit** (`81.6`), NOT as tenths (`816`). This is worth stating because
beestat — the most-cited reference implementation — divides temperature fields
by 10, and copying that here produced readings of 8.16 °F. Whatever source that
divisor is correct for, it is not this endpoint. Values are passed through
unscaled; confirm against a raw row before ever reintroducing a divisor.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOGGER = logging.getLogger(__name__)

BUCKET_SECONDS = 300

# Equipment columns report seconds of runtime within the 5-minute bucket.
EQUIPMENT_COLUMNS = {
    "auxHeat1",
    "auxHeat2",
    "auxHeat3",
    "compCool1",
    "compCool2",
    "compHeat1",
    "compHeat2",
    "dehumidifier",
    "economizer",
    "fan",
    "humidifier",
    "ventilator",
}

# Direct numeric mappings: column -> metric name. No scaling is applied to any
# of them; see the module docstring on units.
NUMERIC_COLUMNS = {
    "zoneAveTemp": "ecobee_zone_temperature_fahrenheit",
    "zoneCoolTemp": "ecobee_zone_cool_setpoint_fahrenheit",
    "zoneHeatTemp": "ecobee_zone_heat_setpoint_fahrenheit",
    "zoneHumidity": "ecobee_zone_humidity_percent",
    "zoneOccupancy": "ecobee_zone_occupancy",
    "outdoorTemp": "ecobee_outdoor_temperature_fahrenheit",
    "outdoorHumidity": "ecobee_outdoor_humidity_percent",
}

# String-valued columns, published as info-style metrics: value 1 with the
# string carried as a label. Cardinality is bounded by the thermostat's own
# vocabulary (home/away/sleep, heat/cool/off).
INFO_COLUMNS = {
    "zoneClimate": ("ecobee_zone_climate_info", "climate"),
    "zoneHvacMode": ("ecobee_zone_hvac_mode_info", "hvac_mode"),
    "hvacMode": ("ecobee_hvac_mode_info", "hvac_mode"),
    "zoneCalendarEvent": ("ecobee_zone_calendar_event_info", "event"),
}

# sensorType -> metric name. Types are read from the response metadata, so an
# account exposing one that is not listed here still produces a metric via the
# ecobee_sensor_value fallback.
#
# dryContact is the door/window contact sensors: 1 open, 0 closed. These DO
# appear in the runtime report, contrary to the common claim that ecobee's
# door/window SmartSensors are invisible to the API.
SENSOR_TYPE_METRICS = {
    "temperature": "ecobee_sensor_temperature_fahrenheit",
    "humidity": "ecobee_sensor_humidity_percent",
    "occupancy": "ecobee_sensor_occupancy",
    "dryContact": "ecobee_sensor_contact",
    "co2": "ecobee_sensor_co2_ppm",
}

_warned_columns: set[str] = set()


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float
    timestamp_ms: int


def _to_utc_ms(date_str: str, time_str: str, tz: ZoneInfo) -> int | None:
    """Convert a row's local `date,time` into a UTC epoch in milliseconds.

    During the autumn DST transition an hour of local times occurs twice.
    `fold=0` selects the first occurrence, which matches the order the report
    lists them in; the alternative would silently shift an hour of data by an
    hour once a year.
    """
    try:
        naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        _LOGGER.warning("Unparseable row timestamp %r %r", date_str, time_str)
        return None
    return int(naive.replace(tzinfo=tz, fold=0).timestamp() * 1000)


def _zone(name: str) -> ZoneInfo:
    """Resolve a thermostat's IANA zone, or fail the cycle.

    Deliberately does NOT fall back to UTC. Rows are in thermostat-local time,
    so the wrong zone shifts every sample by the offset — four hours here — and
    produces data that looks entirely reasonable while being wrong. A failed
    cycle retries in 15 minutes and raises an alert; silently shifted history
    has to be discovered later and re-imported.

    The earlier UTC fallback was worse than useless: on an image with no tz
    database at all, ZoneInfo("UTC") raises too, so the fallback only added a
    second traceback to the first one.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise RuntimeError(
            f"Cannot resolve time zone {name!r}: {err}. If this is a container, "
            f"the tz database is missing — `zoneinfo` reads the SYSTEM database "
            f"and minimal images ship none. The `tzdata` package is a hard "
            f"dependency for exactly this reason."
        ) from err


def _numeric(raw: str) -> float | None:
    """Parse a cell, treating blanks as absent rather than as zero.

    A missing bucket must produce no sample at all. Emitting 0 would be
    indistinguishable from "the compressor ran for zero seconds", which is a
    real and common value.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _warn_once(column: str) -> None:
    if column not in _warned_columns:
        _warned_columns.add(column)
        _LOGGER.info(
            "Column %r has no mapping; skipping it. Units are not documented "
            "well enough to guess — add a mapping in transform.py once verified.",
            column,
        )


def thermostat_samples(
    report: dict[str, Any],
    names: dict[str, str],
    zones: dict[str, str],
) -> Iterable[Sample]:
    """Samples from `reportList` (per-thermostat 5-minute rows)."""
    columns = [c.strip() for c in (report.get("columns") or "").split(",") if c.strip()]

    for entry in report.get("reportList") or []:
        ident = entry.get("thermostatIdentifier", "")
        tz = _zone(zones.get(ident, "UTC"))
        base = {"thermostat": names.get(ident, ident), "thermostat_id": ident}

        for row in entry.get("rowList") or []:
            cells = row.split(",")
            if len(cells) < 2:
                continue
            ts = _to_utc_ms(cells[0], cells[1], tz)
            if ts is None:
                continue

            for index, column in enumerate(columns):
                cell_index = index + 2
                if cell_index >= len(cells):
                    continue
                raw = cells[cell_index].strip()
                if not raw:
                    continue

                if column in INFO_COLUMNS:
                    metric, label = INFO_COLUMNS[column]
                    yield Sample(metric, {**base, label: raw}, 1.0, ts)
                    continue

                value = _numeric(raw)
                if value is None:
                    continue

                if column in EQUIPMENT_COLUMNS:
                    yield Sample(
                        "ecobee_equipment_runtime_seconds",
                        {**base, "equipment": column},
                        value,
                        ts,
                    )
                elif column in NUMERIC_COLUMNS:
                    yield Sample(NUMERIC_COLUMNS[column], dict(base), value, ts)
                else:
                    _warn_once(column)


def sensor_samples(
    report: dict[str, Any],
    names: dict[str, str],
    zones: dict[str, str],
) -> Iterable[Sample]:
    """Samples from `sensorList` (per-remote-sensor 5-minute rows).

    Sensor types are read from the response metadata rather than assumed; an
    account exposing a type this code has never seen still produces a metric,
    via `ecobee_sensor_value`.
    """
    for entry in report.get("sensorList") or []:
        ident = entry.get("thermostatIdentifier", "")
        tz = _zone(zones.get(ident, "UTC"))
        base = {"thermostat": names.get(ident, ident), "thermostat_id": ident}

        metadata = {s["sensorId"]: s for s in entry.get("sensors") or []}
        # The first two columns are date and time; the rest are sensorIds.
        columns = list(entry.get("columns") or [])[2:]

        for row in entry.get("data") or []:
            cells = row.split(",")
            if len(cells) < 2:
                continue
            ts = _to_utc_ms(cells[0], cells[1], tz)
            if ts is None:
                continue

            for index, sensor_id in enumerate(columns):
                cell_index = index + 2
                if cell_index >= len(cells):
                    continue
                value = _numeric(cells[cell_index])
                if value is None:
                    continue

                meta = metadata.get(sensor_id, {})
                sensor_type = meta.get("sensorType", "unknown")
                metric = SENSOR_TYPE_METRICS.get(sensor_type, "ecobee_sensor_value")

                yield Sample(
                    metric,
                    {
                        **base,
                        "sensor": meta.get("sensorName", sensor_id),
                        "sensor_id": sensor_id,
                        "sensor_type": sensor_type,
                        "sensor_usage": meta.get("sensorUsage", "unknown"),
                    },
                    value,
                    ts,
                )


def iter_all_samples(
    report: dict[str, Any],
    names: dict[str, str],
    zones: dict[str, str],
) -> Iterator[Sample]:
    """Yield every sample lazily.

    A generator rather than a list because the caller may be importing 30 days:
    884,384 samples measured at ~514 bytes each is 434 MB of objects, against a
    192Mi container limit. Nothing needs them all at once — they are filtered,
    deduplicated and written in batches — so nothing should hold them all.
    """
    yield from thermostat_samples(report, names, zones)
    yield from sensor_samples(report, names, zones)


def all_samples(
    report: dict[str, Any],
    names: dict[str, str],
    zones: dict[str, str],
) -> list[Sample]:
    """Materialise every sample. Only for a short window — see iter_all_samples."""
    return list(iter_all_samples(report, names, zones))
