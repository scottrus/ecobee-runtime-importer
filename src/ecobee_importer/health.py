"""Self-health metrics.

This endpoint carries facts about *this process* only. No house data is exposed
here — that all goes to VictoriaMetrics with its original timestamps.

These series ARE scraped, so alert rules over them use bare instant selectors.
Do not wrap them in `last_over_time()`: the process holds its gauges between
cycles, so they never go stale, and widening the window would defeat the
staleness rule that `ecobee_last_successful_import_timestamp_seconds` exists to
support. (The imported house metrics are the opposite case and DO need
`last_over_time()`. Both kinds originate in this one workload.)
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server

REAUTH_REQUIRED = Gauge(
    "ecobee_reauth_required",
    "1 when the refresh token has been rejected and a human must re-run the "
    "interactive bootstrap. Collection is stopped until then.",
)

LAST_SUCCESSFUL_IMPORT = Gauge(
    "ecobee_last_successful_import_timestamp_seconds",
    "Unix time of the last cycle that fetched and wrote successfully.",
)

NEWEST_BUCKET = Gauge(
    "ecobee_newest_bucket_timestamp_seconds",
    "Unix time of the newest 5-minute bucket seen for a thermostat. Goes stale "
    "when a thermostat stops reporting to ecobee.",
    ["thermostat"],
)

CYCLES = Counter(
    "ecobee_import_cycles_total",
    "Import cycles, by outcome.",
    ["result"],
)

API_REQUESTS = Counter(
    "ecobee_api_requests_total",
    "Requests issued to ecobee. The audit trail for rate discipline.",
    ["endpoint", "outcome"],
)

SAMPLES_WRITTEN = Counter(
    "ecobee_samples_written_total",
    "Samples accepted by the metrics backend.",
)

TOKEN_REFRESHES = Counter(
    "ecobee_token_refreshes_total",
    "Token refresh attempts, by outcome.",
    ["outcome"],
)

BUILD_INFO = Gauge(
    "ecobee_importer_build_info",
    "Build metadata. Always 1.",
    ["version"],
)


def serve(port: int, version: str) -> None:
    REAUTH_REQUIRED.set(0)
    BUILD_INFO.labels(version=version).set(1)
    start_http_server(port)
