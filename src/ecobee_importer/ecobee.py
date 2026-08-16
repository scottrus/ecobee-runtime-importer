"""ecobee API access.

Authentication is delegated entirely to `python-ecobee-api` — the library Home
Assistant depends on — so that when ecobee changes its Auth0 login the fix
arrives as a version bump rather than as work here.

That library has no `runtimeReport` support, so this module issues that request
itself, but *through* the library's request helper rather than with a bare
`requests.get`. That is a deliberate coupling to a private method, and it is
worth it:

    ecobee does not use HTTP status codes for auth failures.

An expired access token comes back as **HTTP 500** with `status.code == 14`, and
an invalid token as 500 with `status.code` 1 or 16. A hand-rolled client that
retries on 401 will never refresh, and will instead surface every auth failure
as an opaque server error. `_request_with_refresh` already owns that mapping and
the refresh-then-retry-once behaviour built on it. Duplicating the table here
would mean maintaining an undocumented, empirically-derived error contract.

The cost of the coupling is that `python-ecobee-api` is pinned to a minor
version and its release notes are worth reading before bumping.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from pyecobee import Ecobee
from pyecobee.const import (
    ECOBEE_ACCESS_TOKEN,
    ECOBEE_API_KEY,
    ECOBEE_REFRESH_TOKEN,
)
from pyecobee.errors import EcobeeError, ExpiredTokenError, InvalidTokenError

from .tokens import Tokens, TokenStore

_LOGGER = logging.getLogger(__name__)

ENDPOINT_RUNTIME_REPORT = "runtimeReport"

# API hard limits. Exceeding either is an error from ecobee, not a truncation.
MAX_THERMOSTATS_PER_REQUEST = 25
MAX_DAYS_PER_REQUEST = 31


class ReauthRequired(Exception):
    """Terminal authentication failure: a human must repeat the interactive login.

    Raised when the refresh token itself is rejected (`invalid_grant`). Nothing
    the process can do recovers from this — see ARCHITECTURE.md §4.4.
    """


@dataclass
class Thermostat:
    identifier: str
    name: str
    # IANA zone from `location.timeZone`. Report rows arrive in this zone, not
    # UTC — see ARCHITECTURE.md §3.2.
    time_zone: str


class EcobeeClient:
    def __init__(self, store: TokenStore):
        self._store = store
        self._tokens = store.load()
        _LOGGER.info("Loaded credentials: %s", self._tokens.redacted())

        config: dict[str, str] = {
            ECOBEE_ACCESS_TOKEN: self._tokens.access_token,
            ECOBEE_REFRESH_TOKEN: self._tokens.refresh_token,
        }
        # Only set for legacy developer-key accounts, where its presence
        # switches pyecobee to the older PIN refresh endpoint. An empty string
        # would be truthy enough to matter, so the key is omitted entirely.
        if self._tokens.api_key:
            config[ECOBEE_API_KEY] = self._tokens.api_key

        self._api = Ecobee(config=config)

    # --- token lifecycle ---------------------------------------------------

    def refresh(self) -> None:
        """Refresh the access token, persisting a rotated refresh token."""
        try:
            self._api.refresh_tokens()
        except InvalidTokenError as err:
            raise ReauthRequired(str(err)) from err
        except EcobeeError as err:
            raise RuntimeError(f"token refresh failed: {err}") from err
        self._persist_if_changed()

    def _persist_if_changed(self) -> None:
        """Write tokens back only when Auth0 actually rotated the refresh token.

        Auth0 may or may not rotate. Writing every cycle would patch the Secret
        for no reason; never writing would lock the account out on the first
        restart after a rotation.
        """
        new_refresh = self._api.refresh_token or ""
        new_access = self._api.access_token or ""

        if not new_refresh:
            # Persisting an empty value would destroy the only copy of the
            # refresh token, so this is refused rather than written through.
            _LOGGER.error("No refresh_token present after refresh; not persisting")
            return

        rotated = new_refresh != self._tokens.refresh_token
        changed = rotated or new_access != self._tokens.access_token
        if not changed:
            return

        self._tokens = Tokens(
            refresh_token=new_refresh,
            access_token=new_access,
            api_key=self._tokens.api_key,
        )
        if rotated:
            _LOGGER.info("Refresh token was rotated by Auth0; persisting")
        self._store.save(self._tokens)

    def _call(self, endpoint: str, params: dict, what: str) -> Any:
        """Issue a request, mapping ecobee's auth errors onto our own.

        Token rotation is checked after every call because the helper may have
        refreshed internally.
        """
        try:
            return self._api._request_with_refresh("GET", endpoint, what, params=params)
        except InvalidTokenError as err:
            raise ReauthRequired(str(err)) from err
        except ExpiredTokenError as err:
            # Only reachable if the helper's single retry also expired.
            raise RuntimeError(
                f"access token still expired after refresh: {err}"
            ) from err
        finally:
            self._persist_if_changed()

    # --- data --------------------------------------------------------------

    def thermostats(self) -> list[Thermostat]:
        """Identifiers, display names and time zones. One request."""
        try:
            self._api.get_thermostats()
        except InvalidTokenError as err:
            raise ReauthRequired(str(err)) from err
        finally:
            self._persist_if_changed()

        result: list[Thermostat] = []
        for entry in self._api.thermostats or []:
            location = entry.get("location") or {}
            result.append(
                Thermostat(
                    identifier=entry["identifier"],
                    name=entry.get("name") or entry["identifier"],
                    # UTC is a poor default, but it is better than crashing, and
                    # a missing timeZone is visible as a whole-offset data shift.
                    time_zone=location.get("timeZone") or "UTC",
                )
            )
        return result

    def runtime_report(
        self,
        identifiers: list[str],
        start_date: str,
        end_date: str,
        columns: list[str],
        include_sensors: bool = True,
        start_interval: int | None = None,
        end_interval: int | None = None,
    ) -> dict[str, Any]:
        """Fetch runtimeReport for up to 25 thermostats over up to 31 days.

        Dates are `YYYY-MM-DD`; intervals are 0-287 (5-minute buckets in a day).
        """
        if len(identifiers) > MAX_THERMOSTATS_PER_REQUEST:
            raise ValueError(
                f"{len(identifiers)} thermostats exceeds the API limit of "
                f"{MAX_THERMOSTATS_PER_REQUEST} per request"
            )

        body: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "columns": ",".join(columns),
            "selection": {
                "selectionType": "thermostats",
                "selectionMatch": ",".join(identifiers),
            },
        }
        if include_sensors:
            # TOP LEVEL, not inside `selection`. The two ecobee endpoints differ:
            # on GET /1/thermostat, includeSensors is a Selection property; on
            # runtimeReport it is a request parameter. Putting it in `selection`
            # here is accepted silently and returns no sensorList at all — no
            # error, just missing data.
            body["includeSensors"] = True
        if start_interval is not None:
            body["startInterval"] = start_interval
        if end_interval is not None:
            body["endInterval"] = end_interval

        # ecobee GETs take their request document as a `json` query parameter.
        params = {"json": json.dumps(body, separators=(",", ":"))}
        return self._call(ENDPOINT_RUNTIME_REPORT, params, "get runtime report")
