"""Tests for the response handling.

Fixture values are copied from a real runtimeReport response, which is the point:
an earlier version of this file encoded an assumption (temperatures in tenths,
inherited from beestat) that the live API contradicts. Temperatures arrive as
decimal degrees and must not be scaled.
"""

from ecobee_importer.transform import all_samples, sensor_samples, thermostat_samples

NAMES = {"411111111111": "Basement"}
ZONES = {"411111111111": "America/New_York"}

REPORT = {
    "columns": "compCool1,fan,zoneAveTemp,zoneHumidity,zoneClimate,outdoorTemp",
    "reportList": [
        {
            "thermostatIdentifier": "411111111111",
            "rowList": [
                "2026-08-15,00:00:00,300,300,72.1,58,home,84.2",
                # Blank cells: absent data, not zero.
                "2026-08-15,00:05:00,,,,,,",
                "2026-08-15,00:10:00,0,120,71.8,59,sleep,83.9",
            ],
        }
    ],
    "sensorList": [
        {
            "thermostatIdentifier": "411111111111",
            "sensors": [
                {
                    "sensorId": "rs:100:1",
                    "sensorName": "Office",
                    "sensorType": "temperature",
                    "sensorUsage": "indoor",
                },
                {
                    "sensorId": "rs:100:2",
                    "sensorName": "Office",
                    "sensorType": "occupancy",
                    "sensorUsage": "indoor",
                },
                {
                    "sensorId": "dw:100:3",
                    "sensorName": "Front Door",
                    "sensorType": "dryContact",
                    "sensorUsage": "monitor",
                },
                {
                    "sensorId": "rs:200:9",
                    "sensorName": "Mystery",
                    "sensorType": "somethingNew",
                    "sensorUsage": "monitor",
                },
            ],
            "columns": ["date", "time", "rs:100:1", "rs:100:2", "dw:100:3", "rs:200:9"],
            "data": ["2026-08-15,00:00:00,69.3,1,1,42"],
        }
    ],
}


def by_name(samples, name):
    return [s for s in samples if s.name == name]


def test_temperatures_are_passed_through_unscaled():
    """Regression: an inherited /10 turned 72.1 F into 7.21 F."""
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    zone = by_name(samples, "ecobee_zone_temperature_fahrenheit")
    assert [s.value for s in zone] == [72.1, 71.8]

    outdoor = by_name(samples, "ecobee_outdoor_temperature_fahrenheit")
    assert [s.value for s in outdoor] == [84.2, 83.9]


def test_humidity_is_passed_through_unscaled():
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    humidity = by_name(samples, "ecobee_zone_humidity_percent")
    assert [s.value for s in humidity] == [58.0, 59.0]


def test_rows_are_thermostat_local_time():
    """00:00 EDT on 2026-08-15 is 04:00 UTC, not 00:00 UTC."""
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    first = by_name(samples, "ecobee_zone_temperature_fahrenheit")[0]
    assert first.timestamp_ms == 1786766400000


def test_equipment_runtime_is_seconds_per_bucket():
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    equipment = by_name(samples, "ecobee_equipment_runtime_seconds")
    cool = [s for s in equipment if s.labels["equipment"] == "compCool1"]
    # A full bucket, then a genuine zero — both are real values.
    assert [s.value for s in cool] == [300.0, 0.0]


def test_blank_cells_produce_no_sample():
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    # Three rows, but the middle one is entirely blank.
    assert len(by_name(samples, "ecobee_zone_temperature_fahrenheit")) == 2


def test_string_columns_become_info_metrics():
    samples = list(thermostat_samples(REPORT, NAMES, ZONES))
    climate = by_name(samples, "ecobee_zone_climate_info")
    assert [s.labels["climate"] for s in climate] == ["home", "sleep"]
    assert all(s.value == 1.0 for s in climate)


def test_sensor_types_drive_metric_choice():
    samples = list(sensor_samples(REPORT, NAMES, ZONES))
    temp = by_name(samples, "ecobee_sensor_temperature_fahrenheit")
    assert temp[0].value == 69.3
    assert temp[0].labels["sensor"] == "Office"
    assert temp[0].labels["sensor_usage"] == "indoor"

    occupancy = by_name(samples, "ecobee_sensor_occupancy")
    assert occupancy[0].value == 1.0


def test_door_contact_sensors_are_exported():
    """dryContact is the door/window SmartSensors, and they DO reach the API."""
    samples = list(sensor_samples(REPORT, NAMES, ZONES))
    contact = by_name(samples, "ecobee_sensor_contact")
    assert len(contact) == 1
    assert contact[0].labels["sensor"] == "Front Door"
    assert contact[0].value == 1.0


def test_unknown_sensor_type_still_exports():
    """An account exposing a type this code has never seen must not lose data."""
    samples = list(sensor_samples(REPORT, NAMES, ZONES))
    fallback = by_name(samples, "ecobee_sensor_value")
    assert len(fallback) == 1
    assert fallback[0].labels["sensor_type"] == "somethingNew"
    assert fallback[0].value == 42.0


def test_unresolvable_zone_fails_rather_than_shifting_data():
    """A wrong zone shifts every sample by the offset and still looks plausible.

    Failing the cycle is recoverable: it retries in 15 minutes and alerts. A
    silent UTC fallback writes four-hours-wrong history that has to be noticed
    later and re-imported.
    """
    import pytest

    with pytest.raises(RuntimeError, match="tz database|Cannot resolve time zone"):
        list(thermostat_samples(REPORT, NAMES, {"411111111111": "Not/AZone"}))


def test_labels_identify_the_thermostat():
    for sample in all_samples(REPORT, NAMES, ZONES):
        assert sample.labels["thermostat"] == "Basement"
        assert sample.labels["thermostat_id"] == "411111111111"
