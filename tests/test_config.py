"""Tests for configuration parsing and the guards on it."""

import pytest

from ecobee_importer.config import DEFAULT_COLUMNS, Config, ConfigError
from ecobee_importer.transform import Sample
from ecobee_importer.writer import render


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("ECOBEE_"):
            monkeypatch.delenv(key, raising=False)


def test_interval_below_the_documented_floor_is_clamped(monkeypatch):
    """ecobee says 15 minutes; a smaller setting must not reach them."""
    monkeypatch.setenv("ECOBEE_IMPORT_INTERVAL_SECONDS", "60")
    assert Config.from_env().import_interval_seconds == 900


def test_lookback_beyond_the_api_limit_is_clamped(monkeypatch):
    monkeypatch.setenv("ECOBEE_STARTUP_LOOKBACK_HOURS", "9999")
    assert Config.from_env().startup_lookback_hours == 31 * 24


def test_extra_columns_add_to_the_defaults(monkeypatch):
    monkeypatch.setenv("ECOBEE_EXTRA_COLUMNS", "sky, wind")
    columns = Config.from_env().columns
    assert columns[: len(DEFAULT_COLUMNS)] == DEFAULT_COLUMNS
    assert columns[-2:] == ["sky", "wind"]


def test_columns_override_replaces_the_defaults(monkeypatch):
    monkeypatch.setenv("ECOBEE_COLUMNS", "zoneAveTemp,fan")
    assert Config.from_env().columns == ["zoneAveTemp", "fan"]


def test_columns_override_wins_over_extra(monkeypatch):
    monkeypatch.setenv("ECOBEE_COLUMNS", "zoneAveTemp")
    monkeypatch.setenv("ECOBEE_EXTRA_COLUMNS", "sky")
    assert Config.from_env().columns == ["zoneAveTemp"]


def test_extra_labels_are_parsed(monkeypatch):
    monkeypatch.setenv("ECOBEE_EXTRA_LABELS", "site=kukui, env=home")
    assert Config.from_env().extra_labels == {"site": "kukui", "env": "home"}


def test_malformed_extra_labels_are_rejected(monkeypatch):
    monkeypatch.setenv("ECOBEE_EXTRA_LABELS", "site")
    with pytest.raises(ConfigError, match="key=value"):
        Config.from_env()


def test_extra_labels_may_not_shadow_generated_ones(monkeypatch):
    """A static `thermostat` label would silently produce wrong series."""
    monkeypatch.setenv("ECOBEE_EXTRA_LABELS", "thermostat=nope")
    with pytest.raises(ConfigError, match="reserved"):
        Config.from_env()


def test_unknown_token_store_is_rejected(monkeypatch):
    monkeypatch.setenv("ECOBEE_TOKEN_STORE", "s3")
    with pytest.raises(ConfigError, match="ECOBEE_TOKEN_STORE"):
        Config.from_env()


def test_non_url_destination_is_rejected(monkeypatch):
    monkeypatch.setenv("ECOBEE_VM_IMPORT_URL", "victoriametrics:8428")
    with pytest.raises(ConfigError, match="not a URL"):
        Config.from_env()


def test_extra_labels_reach_the_rendered_line():
    sample = Sample(
        "ecobee_zone_temperature_fahrenheit", {"thermostat": "Basement"}, 72.1, 1
    )
    assert 'site="kukui"' in render(sample, {"site": "kukui"})
