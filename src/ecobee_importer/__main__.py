"""Entry point: the import loop.

Shape (ARCHITECTURE.md §5):

    startup -> load tokens -> resolve thermostats
      every 900s: window = [watermark - overlap, now]
                  fetch runtimeReport -> samples -> write -> advance watermark

The loop does not exit on error. A container that crash-loops would fetch on
every boot and blow straight through ecobee's documented 15-minute floor, so
transient failures are counted and retried rather than raised. Only invalid
configuration, at startup, is fatal.
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

from . import __version__
from .config import MAX_REPORT_DAYS, Config, ConfigError
from .ecobee import EcobeeClient, ReauthRequired, Thermostat
from .health import (
    API_REQUESTS,
    CYCLES,
    LAST_SUCCESSFUL_IMPORT,
    NEWEST_BUCKET,
    REAUTH_REQUIRED,
    SAMPLES_WRITTEN,
    TOKEN_REFRESHES,
    serve,
)
from .tokens import build_store
from .transform import all_samples
from .writer import VictoriaMetricsWriter

_LOGGER = logging.getLogger("ecobee_importer")

# Padding on the request window. Rows come back in thermostat-local time while
# the request is expressed in dates, so a day on each side removes every
# boundary and DST edge case; samples outside the real window are filtered after
# conversion.
WINDOW_PAD_DAYS = 1


class Importer:
    def __init__(self, cfg: Config, client=None, writer=None):
        # client/writer are injectable so the loop's behaviour can be tested
        # without ecobee or VictoriaMetrics; production passes neither.
        self.cfg = cfg
        self.client = client or EcobeeClient(build_store(cfg))
        self.writer = writer or VictoriaMetricsWriter(
            cfg.vm_import_url,
            cfg.write_timeout_seconds,
            extra_labels=cfg.extra_labels,
            auth_header_file=cfg.vm_auth_header_file,
        )

        self._thermostats: list[Thermostat] = []
        self._thermostats_fetched_at = 0.0

        # In-memory by design: a restart re-imports the lookback window, which
        # is idempotent, and no persisted watermark can go corrupt.
        self.watermark = datetime.now(UTC) - timedelta(hours=cfg.startup_lookback_hours)
        _LOGGER.info(
            "Starting watermark %s (%dh lookback)",
            self.watermark.isoformat(),
            cfg.startup_lookback_hours,
        )

    def thermostats(self) -> list[Thermostat]:
        age = time.monotonic() - self._thermostats_fetched_at
        if self._thermostats and age < self.cfg.thermostat_cache_seconds:
            return self._thermostats

        try:
            self._thermostats = self.client.thermostats()
            API_REQUESTS.labels(endpoint="thermostat", outcome="success").inc()
        except ReauthRequired:
            API_REQUESTS.labels(endpoint="thermostat", outcome="auth_error").inc()
            raise
        except Exception:
            API_REQUESTS.labels(endpoint="thermostat", outcome="error").inc()
            raise

        self._thermostats_fetched_at = time.monotonic()
        _LOGGER.info(
            "Thermostats: %s",
            ", ".join(f"{t.name} ({t.time_zone})" for t in self._thermostats),
        )
        return self._thermostats

    def cycle(self, since: datetime | None = None, dry_run: bool = False) -> int:
        """Run one import. Returns samples written.

        `dry_run` fetches and transforms but writes nothing and advances
        nothing — for validating credentials, columns and the destination-free
        half of the pipeline before a database exists.
        """
        thermostats = self.thermostats()
        if not thermostats:
            _LOGGER.warning("No thermostats registered to this account")
            return 0

        now = datetime.now(UTC)
        start = since or (self.watermark - timedelta(minutes=self.cfg.overlap_minutes))

        oldest_allowed = now - timedelta(days=MAX_REPORT_DAYS)
        if start < oldest_allowed:
            _LOGGER.warning(
                "Requested window starts %s, older than the API's %d-day limit; "
                "clamping to %s",
                start.isoformat(),
                MAX_REPORT_DAYS,
                oldest_allowed.isoformat(),
            )
            start = oldest_allowed

        identifiers = [t.identifier for t in thermostats]
        names = {t.identifier: t.name for t in thermostats}
        zones = {t.identifier: t.time_zone for t in thermostats}

        start_date = (start - timedelta(days=WINDOW_PAD_DAYS)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=WINDOW_PAD_DAYS)).strftime("%Y-%m-%d")

        _LOGGER.info(
            "Fetching runtimeReport %s..%s for %d thermostat(s)",
            start_date,
            end_date,
            len(identifiers),
        )
        try:
            report = self.client.runtime_report(
                identifiers,
                start_date,
                end_date,
                self.cfg.columns,
                include_sensors=self.cfg.include_sensors,
            )
            API_REQUESTS.labels(endpoint="runtimeReport", outcome="success").inc()
        except ReauthRequired:
            API_REQUESTS.labels(endpoint="runtimeReport", outcome="auth_error").inc()
            raise
        except Exception:
            API_REQUESTS.labels(endpoint="runtimeReport", outcome="error").inc()
            raise

        start_ms = int(start.timestamp() * 1000)
        now_ms = int(now.timestamp() * 1000)
        samples = [
            s
            for s in all_samples(report, names, zones)
            if start_ms <= s.timestamp_ms <= now_ms
        ]

        if not samples:
            _LOGGER.info("No new buckets in window")
            return 0

        if dry_run:
            _summarize(report, samples)
            return 0

        written = self.writer.write(samples)
        SAMPLES_WRITTEN.inc(written)

        # Advance only after a successful write, so a VictoriaMetrics outage is
        # retried rather than skipped over.
        newest = max(s.timestamp_ms for s in samples)
        newest_dt = datetime.fromtimestamp(newest / 1000, tz=UTC)
        if since is None:
            self.watermark = max(self.watermark, newest_dt)

        for thermostat in thermostats:
            per_stat = [
                s.timestamp_ms
                for s in samples
                if s.labels.get("thermostat") == thermostat.name
            ]
            if per_stat:
                NEWEST_BUCKET.labels(thermostat=thermostat.name).set(max(per_stat) / 1000)

        _LOGGER.info(
            "Imported %d samples, newest bucket %s", written, newest_dt.isoformat()
        )
        return written

    def run(self) -> None:
        stopping = False

        def stop(signum, _frame):
            nonlocal stopping
            _LOGGER.info("Signal %s received; stopping after this cycle", signum)
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        # A short jitter avoids every replica of every restart landing on the
        # same second against ecobee.
        time.sleep(random.uniform(0, 5))

        while not stopping:
            started = time.monotonic()
            try:
                self.cycle()
                CYCLES.labels(result="success").inc()
                LAST_SUCCESSFUL_IMPORT.set(time.time())
                REAUTH_REQUIRED.set(0)
                TOKEN_REFRESHES.labels(outcome="success").inc(0)
            except ReauthRequired as err:
                # Deliberately not fatal: exiting would add a CrashLoopBackOff
                # to an incident that already needs a human, and would stop the
                # alert from being served.
                REAUTH_REQUIRED.set(1)
                TOKEN_REFRESHES.labels(outcome="invalid_grant").inc()
                CYCLES.labels(result="auth_error").inc()
                _LOGGER.error(
                    "Re-authentication required (%s). Run `make reauth`, or mint a "
                    "token and update the credential store by hand.",
                    err,
                )
                # Re-read the store so that fixing the Secret is sufficient on
                # its own. Without this the rejected token is held in memory for
                # the life of the process, and an operator who corrects the
                # Secret perfectly watches it keep failing until a restart.
                try:
                    if self.client.reload():
                        _LOGGER.info(
                            "Credential store now holds a different token; "
                            "retrying on the next cycle. No restart needed."
                        )
                    else:
                        _LOGGER.error(
                            "The credential store still holds the token that was "
                            "just rejected. Collection stays stopped until it is "
                            "replaced; this process stays up to serve the alert."
                        )
                except Exception:
                    # A store that cannot be read is a separate problem, and it
                    # must not take down the process that is reporting the first.
                    _LOGGER.exception("Could not re-read the credential store")
            except Exception:
                CYCLES.labels(result="error").inc()
                _LOGGER.exception("Import cycle failed; retrying next cycle")

            elapsed = time.monotonic() - started
            remaining = max(0.0, self.cfg.import_interval_seconds - elapsed)
            deadline = time.monotonic() + remaining
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

        _LOGGER.info("Stopped")


def _summarize(report: dict, samples: list) -> None:
    """Print what a cycle would have written. Used by --dry-run."""
    from collections import Counter

    print("\n--- sensors reported by this account " + "-" * 34)
    found = False
    for entry in report.get("sensorList") or []:
        for meta in entry.get("sensors") or []:
            found = True
            print(
                f"  {meta.get('sensorName', '?'):<24} "
                f"type={meta.get('sensorType', '?'):<14} "
                f"usage={meta.get('sensorUsage', '?')}"
            )
    if not found:
        print("  (none — includeSensors returned no sensor metadata)")

    print("\n--- samples by metric " + "-" * 49)
    for name, count in sorted(Counter(s.name for s in samples).items()):
        print(f"  {count:>7}  {name}")

    print("\n--- newest sample per thermostat " + "-" * 38)
    newest: dict[str, int] = {}
    for sample in samples:
        key = sample.labels.get("thermostat", "?")
        newest[key] = max(newest.get(key, 0), sample.timestamp_ms)
    for name, ts in sorted(newest.items()):
        stamp = datetime.fromtimestamp(ts / 1000, tz=UTC)
        print(f"  {name:<24} {stamp.isoformat()}  ({_ago(stamp)} ago)")

    print("\n--- example lines, as they would be written " + "-" * 27)
    from .writer import render

    # One per metric rather than the first N: the first N are all the same
    # metric, which hides exactly the values worth eyeballing (a temperature
    # that reads 721 means the tenths divisor is missing).
    seen: set[str] = set()
    for sample in samples:
        if sample.name in seen:
            continue
        seen.add(sample.name)
        print(f"  {render(sample)}")
    print(f"\n{len(samples)} samples would be written. Nothing was sent.\n")


def _ago(stamp: datetime) -> str:
    delta = datetime.now(UTC) - stamp
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def main() -> int:
    parser = argparse.ArgumentParser(prog="ecobee-runtime-importer")
    parser.add_argument(
        "--version",
        action="version",
        version=f"ecobee-runtime-importer {__version__}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch one window, transform it, print a summary, and exit without "
        "writing anything. Validates credentials and columns before a metrics "
        "backend exists.",
    )
    parser.add_argument(
        "--backfill-from",
        metavar="YYYY-MM-DD",
        help="Import a single window from this date to now, then exit. Use to "
        "recover an outage longer than the startup lookback. Subject to the "
        f"API's {MAX_REPORT_DAYS}-day limit.",
    )
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.dry_run:
        Importer(cfg).cycle(dry_run=True)
        return 0

    if args.backfill_from:
        try:
            since = datetime.strptime(args.backfill_from, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            print("--backfill-from must be YYYY-MM-DD", file=sys.stderr)
            return 2
        importer = Importer(cfg)
        written = importer.cycle(since=since)
        _LOGGER.info("Backfill complete: %d samples", written)
        return 0

    # Credentials are loaded before the metrics server starts, and a failure here
    # is fatal rather than retried. That does NOT contradict §5's "the loop never
    # exits": this failure happens before any ecobee request, so a restart loop
    # cannot reach their API and cannot breach the rate floor. A missing or
    # malformed credential needs a human, and a crash-looping pod is a louder
    # signal than a process that runs while collecting nothing.
    try:
        importer = Importer(cfg)
    except (FileNotFoundError, PermissionError, ValueError, RuntimeError) as err:
        print(f"startup failed: {err}", file=sys.stderr)
        return 2

    serve(cfg.metrics_port, __version__)
    _LOGGER.info(
        "Serving self-health metrics on :%d; import interval %ds",
        cfg.metrics_port,
        cfg.import_interval_seconds,
    )
    importer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
