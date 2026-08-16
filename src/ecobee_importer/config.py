"""Configuration, entirely from the environment.

Every knob has a default that is safe to run with. The one value that is clamped
rather than trusted is the import interval: ecobee's documentation says "DO NOT
request report data at an interval quicker than once every 15 minutes", so a
smaller setting is raised rather than honoured.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# ecobee's documented floor for runtimeReport. Not a tunable.
MIN_IMPORT_INTERVAL_SECONDS = 900

# The API refuses report windows longer than this.
MAX_REPORT_DAYS = 31

DEFAULT_COLUMNS = [
    # Equipment runtime, in seconds per 5-minute bucket.
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
    # Environment.
    "zoneAveTemp",
    "zoneCoolTemp",
    "zoneHeatTemp",
    "zoneHumidity",
    "zoneClimate",
    "zoneHvacMode",
    "zoneOccupancy",
    "outdoorTemp",
    "outdoorHumidity",
]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ConfigError(f"{name}={raw!r} is not an integer") from err


def _labels(raw: str) -> dict[str, str]:
    """Parse `key=value,key2=value2` into label pairs."""
    labels: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(f"ECOBEE_EXTRA_LABELS entry {pair!r} is not key=value")
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ConfigError(f"ECOBEE_EXTRA_LABELS entry {pair!r} has an empty side")
        labels[key] = value
    return labels


class ConfigError(Exception):
    """Raised for configuration that cannot produce a working process."""


@dataclass
class Config:
    # --- where tokens live -------------------------------------------------
    # "file" for local development, "kubernetes" in the cluster.
    token_store: str = "file"
    token_file: str = "/var/lib/ecobee/credentials.json"
    secret_name: str = "ecobee-importer-tokens"
    # Empty means "the namespace this pod is running in", read from the
    # ServiceAccount mount. Leaving it unset is the right answer in almost every
    # deployment; setting it is only for reading a Secret from elsewhere, which
    # also needs the RBAC to be a ClusterRole.
    secret_namespace: str = ""

    # --- where samples go --------------------------------------------------
    vm_import_url: str = "http://victoriametrics:8428/api/v1/import/prometheus"
    write_timeout_seconds: int = 60
    # Path to a file whose contents become the Authorization header on writes.
    # A path rather than a value so the credential can come from a Secret mount
    # and never appear in a ConfigMap. Re-read per write, so rotation needs no
    # restart.
    vm_auth_header_file: str = ""

    # --- pacing ------------------------------------------------------------
    import_interval_seconds: int = MIN_IMPORT_INTERVAL_SECONDS
    startup_lookback_hours: int = 24
    overlap_minutes: int = 60
    thermostat_cache_seconds: int = 3600

    # --- what to collect ---------------------------------------------------
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    include_sensors: bool = True
    # Static labels added to every sample, e.g. {"site": "kukui"}. For estates
    # running more than one instance against more than one ecobee account.
    extra_labels: dict[str, str] = field(default_factory=dict)

    # --- process -----------------------------------------------------------
    metrics_port: int = 9863
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            token_store=os.environ.get("ECOBEE_TOKEN_STORE", cls.token_store).lower(),
            token_file=os.environ.get("ECOBEE_TOKEN_FILE", cls.token_file),
            secret_name=os.environ.get("ECOBEE_SECRET_NAME", cls.secret_name),
            secret_namespace=os.environ.get(
                "ECOBEE_SECRET_NAMESPACE", cls.secret_namespace
            ),
            vm_import_url=os.environ.get("ECOBEE_VM_IMPORT_URL", cls.vm_import_url),
            vm_auth_header_file=os.environ.get(
                "ECOBEE_VM_AUTH_HEADER_FILE", cls.vm_auth_header_file
            ),
            write_timeout_seconds=_int("ECOBEE_WRITE_TIMEOUT_SECONDS", 60),
            import_interval_seconds=_int(
                "ECOBEE_IMPORT_INTERVAL_SECONDS", MIN_IMPORT_INTERVAL_SECONDS
            ),
            startup_lookback_hours=_int("ECOBEE_STARTUP_LOOKBACK_HOURS", 24),
            overlap_minutes=_int("ECOBEE_OVERLAP_MINUTES", 60),
            thermostat_cache_seconds=_int("ECOBEE_THERMOSTAT_CACHE_SECONDS", 3600),
            include_sensors=os.environ.get("ECOBEE_INCLUDE_SENSORS", "true").lower()
            != "false",
            metrics_port=_int("ECOBEE_METRICS_PORT", 9863),
            log_level=os.environ.get("ECOBEE_LOG_LEVEL", "INFO").upper(),
        )

        # Full override wins over the additive form, so an operator can pin an
        # exact column set rather than inheriting future defaults.
        override = os.environ.get("ECOBEE_COLUMNS", "").strip()
        extra = os.environ.get("ECOBEE_EXTRA_COLUMNS", "").strip()
        if override:
            cfg.columns = [c.strip() for c in override.split(",") if c.strip()]
            if extra:
                _LOGGER.warning("ECOBEE_COLUMNS is set; ignoring ECOBEE_EXTRA_COLUMNS")
        elif extra:
            cfg.columns = list(DEFAULT_COLUMNS) + [
                c.strip() for c in extra.split(",") if c.strip()
            ]

        cfg.extra_labels = _labels(os.environ.get("ECOBEE_EXTRA_LABELS", ""))

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.token_store not in ("file", "kubernetes"):
            raise ConfigError(
                f"ECOBEE_TOKEN_STORE={self.token_store!r}; "
                "expected 'file' or 'kubernetes'"
            )

        if self.import_interval_seconds < MIN_IMPORT_INTERVAL_SECONDS:
            # Clamped rather than rejected: a too-eager setting should not stop
            # collection, but it must not reach ecobee either.
            _LOGGER.warning(
                "ECOBEE_IMPORT_INTERVAL_SECONDS=%d is below ecobee's documented "
                "floor of %d; raising it",
                self.import_interval_seconds,
                MIN_IMPORT_INTERVAL_SECONDS,
            )
            self.import_interval_seconds = MIN_IMPORT_INTERVAL_SECONDS

        max_lookback = MAX_REPORT_DAYS * 24
        if self.startup_lookback_hours > max_lookback:
            _LOGGER.warning(
                "ECOBEE_STARTUP_LOOKBACK_HOURS=%d exceeds the API's %d-day limit; "
                "reducing to %d",
                self.startup_lookback_hours,
                MAX_REPORT_DAYS,
                max_lookback,
            )
            self.startup_lookback_hours = max_lookback

        if self.startup_lookback_hours < 1:
            raise ConfigError("ECOBEE_STARTUP_LOOKBACK_HOURS must be at least 1")

        if not self.vm_import_url.startswith(("http://", "https://")):
            raise ConfigError(f"ECOBEE_VM_IMPORT_URL={self.vm_import_url!r} is not a URL")

        # A static label that collides with a generated one would be silently
        # overwritten, producing series that look right and are not.
        reserved = {
            "thermostat",
            "thermostat_id",
            "equipment",
            "sensor",
            "sensor_id",
            "sensor_type",
            "sensor_usage",
            "climate",
            "hvac_mode",
            "event",
        }
        clashes = reserved & set(self.extra_labels)
        if clashes:
            raise ConfigError(
                f"ECOBEE_EXTRA_LABELS may not use reserved label(s): "
                f"{', '.join(sorted(clashes))}"
            )
