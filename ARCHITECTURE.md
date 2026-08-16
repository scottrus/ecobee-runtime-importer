# Architecture

`ecobee-runtime-importer` pulls historical HVAC data from ecobee's `runtimeReport`
endpoint and writes it into VictoriaMetrics with its original timestamps.

It is an **importer**, not an exporter. It does not expose house data on a `/metrics`
endpoint and nothing scrapes it for temperatures. Understanding why is most of this
document.

---

## 1. The constraints that shape everything

Three external facts drive every decision below. None of them are negotiable, and a
design that ignores any one of them fails in a way that needs a human to repair.

### 1.1 ecobee issues no new API keys

The developer portal states: *"Sorry, we are not currently accepting new developer
registrations at this time."* Every published ecobee exporter requires you to bring your
own developer API key, so all of them are unusable for anyone who did not register before
the portal closed.

The way in is the one Home Assistant adopted in its 2026.3 release: authenticate against
ecobee's Auth0 tenant using **ecobee's own web application client**, which needs no
developer registration.

```
client_id     183eORFPlXyz9BbDZwqexHPBQoVjgadh
authorize     https://auth.ecobee.com/authorize      (PKCE, S256)
token         https://auth.ecobee.com/oauth/token
scope         openid offline_access smartWrite piiWrite piiRead smartRead deleteGrants
```

The resulting Auth0 access token is used **directly** as the bearer against
`api.ecobee.com/1/*`. There is no second token exchange.

This is not a sanctioned integration path. It is what Home Assistant ships, so it is
well-trodden, but ecobee has promised nothing and can break it without notice. See §9.

### 1.2 The initial login cannot be automated

The login walks Auth0's hosted forms — `/u/login/identifier`, `/u/login/password`, then
`/u/mfa-otp-challenge` if TOTP is enabled. A human with an authenticator app is required.

Therefore **bootstrap is a one-shot, human-run operation** (§4.1) and the long-running
process never sees a password. Anything that tries to schedule the login is misdesigned.

### 1.3 ecobee's documented rate rule for this endpoint

> DO NOT request report data at an interval quicker than once every 15 minutes.

This is an explicit instruction, not a courtesy. The import loop's floor is 900 seconds
and the code refuses to go below it (§8).

---

## 2. Why `runtimeReport` and not live polling

The obvious design — poll `GET /1/thermostat` every few minutes and expose a `/metrics`
endpoint — is worse on every axis that matters here.

| | Live poll (`/1/thermostat`) | `runtimeReport` |
|---|---|---|
| Zone temp / humidity | current value | 5-minute buckets |
| Setpoints, HVAC mode, climate | current | 5-minute buckets |
| Outdoor temp / humidity | current | 5-minute buckets |
| Occupancy | current | 5-minute buckets |
| **Equipment runtime** | a boolean: "running right now" | **seconds of runtime per bucket** |
| Recovery after an outage | data is gone permanently | fully recoverable |

Two rows carry the argument.

**Equipment runtime.** A thermostat uploads to ecobee's cloud roughly every 15 minutes, so
a 3-minute poll returns the same value five times and short cycles vanish between samples.
Duty cycle cannot be reconstructed from that. `runtimeReport` reports seconds-of-runtime
per 5-minute bucket directly, which is the measurement most HVAC questions actually need.

**Recovery.** A scraper that is down loses data forever. An importer that is down catches
up on its next run. Since the report is retrievable retroactively for up to 31 days, a pod
eviction, a node drain, or a two-hour cluster outage costs nothing at all. This is the
single largest robustness win in the design and it is unavailable to any scrape-based
approach.

What live polling would add — instantaneous state, `connected` status, active holds — is
real but belongs to a different use case: asking "what is it doing right now?"
interactively. That is [ecobee-mcp](https://github.com/emrikol/ecobee-mcp)'s job, not this
service's (§10.2).

**Freshness is a smaller advantage than expected.** Measured against a live account, the
newest bucket returned by `runtimeReport` was the *current* 5-minute bucket, under a minute
old — not the tens of minutes assumed while designing this. Trailing buckets do arrive
partially filled (outdoor columns populate ahead of zone columns), so anything wanting
sub-5-minute resolution still needs a live path, but "the report is well behind real time"
is not true.

---

## 3. The data

### 3.1 Request

`GET /1/runtimeReport` with `selectionType=thermostats` and a CSV of thermostat
identifiers. Limits: **25 thermostats** and **31 days** per request. Every thermostat on the account
is fetched in a single request, so a typical household is one request per cycle.

Requested columns (the well-understood subset):

```
auxHeat1, auxHeat2, auxHeat3, compCool1, compCool2, compHeat1, compHeat2,
dehumidifier, economizer, fan, humidifier, ventilator,
zoneAveTemp, zoneCoolTemp, zoneHeatTemp, zoneHumidity, zoneClimate,
zoneHvacMode, zoneOccupancy, outdoorTemp, outdoorHumidity
```

`sky`, `wind`, `dmOffset`, `zoneHumidityHigh/Low`, `zoneCalendarEvent` and `hvacMode` are
available but **deliberately not requested by default**. Their units or semantics are not
documented well enough to publish as metrics without guessing, and a metric with a guessed
unit is worse than no metric. They can be enabled via `ECOBEE_EXTRA_COLUMNS` once verified
against real data.

`includeSensors=true` adds a `sensorList` of `RuntimeSensorReport` objects — the same
5-minute rows, where each column *is* a `sensorId`, accompanied by metadata giving
`sensorName`, `sensorType` (`co2, ctclamp, dryContact, humidity, plug, temperature`) and
`sensorUsage` (`dischargeAir, indoor, monitor, outdoor`).

**`includeSensors` goes at the top level of the request body, not inside `selection`.**
The two endpoints differ: on `GET /1/thermostat` it is a Selection property, on
`runtimeReport` it is a request parameter. Placing it in `selection` here is accepted
silently and returns no `sensorList` at all — no error, no warning, just a response that
looks like an account with no remote sensors.

Sensor types are **enumerated from the response, never hardcoded**. What a given account
exposes is an empirical question, and that decision paid for itself immediately: the first
live run returned seven `dryContact` sensors — the door and window SmartSensors — which
the code exported with no change at all. See §10.3.

### 3.2 Traps in the response

**Temperatures are decimal degrees Fahrenheit — do NOT scale them.** Verified against a
live response: `outdoorTemp` arrives as `81.6`, a remote sensor as `74.6`. Humidity is a
whole-number percentage.

This is called out because the obvious reference implementation disagrees. beestat divides
its temperature fields by 10, and inheriting that divisor here produced indoor readings of
`7.37 °F` — wrong by an order of magnitude, but *self-consistent* across every temperature
column, which is exactly the kind of error that survives a code review and gets caught only
by looking at one rendered sample. Whatever source beestat's divisor is right for, it is
not this endpoint. Confirm against a raw row before reintroducing any scaling.

**Row timestamps are in the thermostat's local time, not UTC.** The request's
`startDate`/`endDate` are documented as UTC, but each row's `date,time` prefix is local to
the thermostat. Every row must be converted using that thermostat's
`location.timeZone` before it becomes a sample timestamp. A naive implementation silently
shifts every sample by that zone's UTC offset — and the data looks entirely reasonable
until it is compared against anything else. Accounts spanning zones make it worse: the
error differs per thermostat, so the series drift relative to each other.

The importer requests a window padded by one day on each side and filters after
conversion, so daylight-saving transitions and window boundaries cannot produce edge cases.

### 3.3 Metrics produced

No scaling is applied to any value.

| Metric | Source | Notes |
|---|---|---|
| `ecobee_zone_temperature_fahrenheit` | `zoneAveTemp` | the zone **average**, not one sensor — see below |
| `ecobee_zone_humidity_percent` | `zoneHumidity` | |
| `ecobee_zone_heat_setpoint_fahrenheit` | `zoneHeatTemp` | |
| `ecobee_zone_cool_setpoint_fahrenheit` | `zoneCoolTemp` | |
| `ecobee_zone_occupancy` | `zoneOccupancy` | as reported |
| `ecobee_outdoor_temperature_fahrenheit` | `outdoorTemp` | |
| `ecobee_outdoor_humidity_percent` | `outdoorHumidity` | |
| `ecobee_equipment_runtime_seconds` | equipment columns | label `equipment`, **seconds per 5-min bucket** |
| `ecobee_zone_climate_info` | `zoneClimate` | value 1, label `climate` |
| `ecobee_zone_hvac_mode_info` | `zoneHvacMode` | value 1, label `hvac_mode` |
| `ecobee_sensor_temperature_fahrenheit` | sensor report, `temperature` | |
| `ecobee_sensor_humidity_percent` | sensor report, `humidity` | the thermostats' own sensors |
| `ecobee_sensor_occupancy` | sensor report, `occupancy` | |
| `ecobee_sensor_contact` | sensor report, `dryContact` | doors and windows; 1 open, 0 closed |
| `ecobee_sensor_value` | sensor report | fallback for unrecognised `sensorType` |

All carry `thermostat` (the user-assigned name) and `thermostat_id`. Sensor metrics also
carry `sensor`, `sensor_type` and `sensor_usage`.

`ecobee_equipment_runtime_seconds` is a **gauge of seconds per bucket**, not a counter.
Duty cycle is `ecobee_equipment_runtime_seconds / 300`. Do not wrap it in `rate()`.

**`zoneAveTemp` is an average across participating sensors, not a thermostat reading.**
The name says so and the metric name hides it. Measured on a live account, the zone value
sits 0.3–1.2 °F away from the thermostat's own sensor. The two are different quantities and
each is right for a different question:

| Question | Use |
|---|---|
| What is the thermostat controlling against? | `ecobee_zone_temperature_fahrenheit` — the setpoint comparison is only meaningful against the value the thermostat acts on |
| What is the temperature *at* the thermostat? | `ecobee_sensor_temperature_fahrenheit{sensor_id="ei:0:1"}` |
| What is the temperature in room X? | the `ecobee_sensor_temperature_fahrenheit` series for that sensor |

This matters most for **dewpoint**, which pairs temperature with humidity. `zoneHumidity` is
measured at the thermostat — it matches the `ei:0:2` sensor exactly — so combining it with
the zone *average* temperature pairs a single-point humidity with a multi-point
temperature. Use the thermostat's own sensor for both, or accept an error that grows with
how much the rooms disagree.

Remote sensor IDs follow `deviceName:deviceId:sensorId`. The thermostat's own sensors
appear as the equipment interface — `ei:0:1` temperature, `ei:0:2` humidity — while remote
SmartSensors appear as `rs:`/`rs2:` and door/window contacts as `dw:`.

**There is no per-room humidity.** Remote SmartSensors measure temperature and occupancy;
humidity is measured only at the thermostats themselves. Room-level dewpoint analysis is
therefore limited to wherever a thermostat is mounted, not to every sensor location.

---

## 4. Credentials

This is the part that breaks if it is designed casually, so it gets its own section.

### 4.1 Bootstrap — human, once

`scripts/bootstrap.py` performs the interactive Auth0 login via `python-ecobee-api` and
prints the resulting refresh token. The operator stores it in a password manager and
creates the Kubernetes Secret from it.

The library — the one Home Assistant depends on — owns the fragile part. When ecobee
changes its login forms, the fix arrives as a version bump rather than as work in this
repo. That is the entire reason this project is Python.

**Username and password are never stored** and never reach the running service. They exist
only in the operator's terminal during bootstrap.

### 4.2 Steady state — one writer, in-process

The refresh grant is a single, stable, documented call:

```
POST https://auth.ecobee.com/oauth/token
  grant_type=refresh_token & refresh_token=<...> & client_id=183eORFPlXyz9BbDZwqexHPBQoVjgadh
```

The importer performs it lazily: before the first request when no access token was loaded,
and whenever the API reports an expired one. There is no scheduled refresh and no separate
refresher component.

Note that ecobee does **not** use 401 for this — see §4.4 and §9.

**Why there is no credential-refresh CronJob.** Auth0 may rotate the refresh token on
every use — `python-ecobee-api` handles both cases because ecobee's behaviour here is not
guaranteed either way. If rotation is on and two processes both refresh, the second one
presents a dead token, receives `invalid_grant`, and authentication is finished until a
human repeats §4.1 with an authenticator app. Splitting refresh into a CronJob creates
exactly that race for no benefit. **Exactly one process may ever write tokens.**

That property is why §10.2 matters: any additional consumer must be a reader.

### 4.3 Persistence

Auth0 may return a rotated refresh token on any refresh. If that value lives only in
memory, the next pod restart presents the stale token from the Secret and locks the
account out. So after every refresh the importer compares the refresh token to what it
loaded and, if it changed, **writes it back to the Kubernetes Secret** (a strategic-merge
PATCH via the in-cluster ServiceAccount, RBAC scoped to that one Secret by `resourceNames`).

A consequence worth writing down, because it inverts the usual convention for credential
Secrets: **this Secret is mutable state owned by the workload, not a static copy of a
password-manager item.** After the first rotation the cluster's value and any copy you
kept differ, and the cluster's is authoritative. Restoring the saved copy presents a
revoked token and locks the account out; back it up by reading it out of the cluster.

This also rules out managing it with GitOps or templating it from the Helm chart — both
would reassert a stale value on every sync or upgrade. The chart deliberately refuses to
render a Secret, and `make helm` fails if a template ever does.

A `file` store backend exists for local development and carries the same semantics.

The Secret's name is the one setting that appears in two places — the ConfigMap
and the Role's `resourceNames` — because scoping the Role by name is what stops
`patch` on secrets meaning *every* secret in the namespace. That coupling is
worth the narrower grant, but it fails as an unexplained 403, so the store
detects that status specifically and says which file disagrees. The namespace,
by contrast, is not configured at all by default: it resolves from the pod's own
ServiceAccount mount, so moving the workload needs no config change.

### 4.4 An empty access token is not an expired one

The Secret holds only `refresh_token` — the access token is short-lived and there is no
reason to seed it — so a fresh pod always starts with no access token.

**Calling ecobee with an empty bearer does not produce the "expired" path.** Status code
14 is what makes `_request_with_refresh` refresh and retry; an empty token returns 1 or 16,
"invalid", which maps to `InvalidTokenError` and is indistinguishable from a genuinely dead
refresh token. The importer therefore reported `ecobee_reauth_required` against a
credential minted seconds earlier, having never attempted a refresh at all.

So the first request is preceded by an explicit refresh whenever no access token was
loaded. The doomed call is not made.

This is invisible from a `credentials.json`, which carries both tokens — every local run
and every dry-run skipped the path entirely. The condition only exists where the
credential comes from the Secret, which is to say only in the deployment.

### 4.5 When it does break

An `invalid_grant` is unrecoverable without a human. The process does **not** exit — it
sets `ecobee_reauth_required 1`, keeps serving metrics, and keeps that alert firing until
someone re-runs the bootstrap. Crashing would only add a CrashLoopBackOff to an incident
that already needs hands.

**It also re-reads the credential store on every such failure**, so the human's part ends
at replacing the Secret. Without that the rejected token lives in memory for the life of
the process: an operator could correct the Secret perfectly and watch the importer keep
failing against the old value, with a pod restart as the only cure. That is a bad shape
for a recovery path — the fix appears not to work, which invites people to go looking for
a second problem that does not exist.

The reload's return value drives the log, and the distinction matters during an incident:
a changed token says *retrying next cycle*, an unchanged one says *the store still holds
what was just rejected* — which means the update did not land where the importer reads.

---

## 5. The import loop

```
startup → load tokens → resolve thermostats (identifiers, names, time zones)
  ↓
  ├── every 900s ──→ window = [watermark - overlap, now]
  │                  GET /1/runtimeReport (+includeSensors)
  │                  rows → local time → UTC → samples   (streamed, never a list)
  │                  drop unchanged → batch → POST to VictoriaMetrics
  │                  advance watermark
  ↓
/metrics (self-health only, scraped)
```

**No persisted watermark.** It lives in memory, seeded on startup from
`ECOBEE_STARTUP_LOOKBACK_HOURS` (default 24, capped at the API's 31 days). A restart
therefore re-imports the last day, which is harmless because the write is idempotent
(§6), and it removes an entire class of state-corruption bugs. Outages longer than the
lookback are handled by raising it or by a one-off `--backfill-from` run.

**Deliberate overlap.** Each cycle re-requests `ECOBEE_OVERLAP_MINUTES` (default 60)
before the watermark. ecobee fills in late and occasionally revises recent buckets; the
overlap picks those corrections up.

**Re-importing an identical sample is NOT free**, which an earlier version of this
document asserted. VictoriaMetrics stores duplicate samples unless deduplication is
configured, and raw-sample functions then over-count: with a 60-minute overlap and a
15-minute interval every bucket is offered four times, so `count_over_time` and
`sum_over_time` inflated ~4x. Measured on real data, that read as **50 hours of
compressor runtime in a 24-hour day** — the impossible number is what makes it
detectable, and a subtler ratio would not have been.

So the importer keeps the overlap and sends only what is new or **whose value has
changed**. Suppressing on presence rather than value would discard exactly the
revisions the overlap exists to collect. `ecobee_samples_skipped_total` counts the
suppressed writes.

The cache is in memory, so a restart re-writes its lookback window. That is the same
trade as the watermark (§5): persisting it would reintroduce a state-corruption class,
and restarts are rare. It is also why queries over history that spans restarts should
still prefer `sum_over_time(last_over_time(...)[...])` over the raw form.

**The loop never exits on error.** Transient failures are counted, logged, and retried on
the next cycle. Only invalid configuration at startup is fatal. This is what keeps the
process from crash-looping into ecobee's rate limit — a container that restarts every 30
seconds and fetches on boot would violate §1.3 no matter what the interval setting says.

**Samples are streamed, never materialised.** The transform is a generator and the loop
writes in batches of 10,000, so peak memory does not scale with the window length.

That is not a micro-optimisation. A 30-day rebuild is ~884,000 samples at ~514 bytes
each — **434 MB of objects against a 192Mi container limit**, which OOMKilled. Measured on
a synthetic 30-day report: 224 MB materialised versus **0.4 MB streamed**, a 564x
reduction. A full 30-day rebuild originally required a temporary 2Gi limit; it no longer
does.

The window can therefore be as long as the API allows (31 days) without a memory
consequence. That matters because this history is **reconstructible** — `runtimeReport`
serves 31 days retroactively, so deleting the imported series and re-importing is a valid
repair for corruption, a gap, or duplication. Metrics are not usually recoverable that
way, and a repair should not need a deployment change to run.

**Thermostat metadata is cached for an hour.** Identifiers, display names and time zones
come from `GET /1/thermostat`, refreshed hourly — one call per hour, which also surfaces
newly added thermostats without a restart.

---

## 6. The write path

Samples carry their **own** timestamps, so this is backfill, not scraping. VictoriaMetrics
accepts out-of-order and historical writes without limitation inside the retention period.

Default backend is VM's `/api/v1/import/prometheus`, which takes Prometheus exposition
text with an explicit millisecond timestamp per sample:

```
ecobee_zone_temperature_fahrenheit{thermostat="Downstairs",thermostat_id="4117..."} 72.1 1755302400000
```

Chosen over remote-write because it needs no protobuf or snappy dependency, which keeps
the container small and the failure modes readable.

**Re-importing is safe, but it is not free.** Writing a bucket again produces a second
raw sample at the same timestamp. Value queries still return one value, so the overlap and
the restart lookback remain correct — but `count_over_time` and `sum_over_time` count raw
samples, so they inflate. That is why the importer suppresses unchanged re-writes (§5), and
why a storage-side deduplication setting is worth having as well: the two cover different
cases, and only the second covers a restart.

VictoriaMetrics keeps one sample per series per `-dedup.minScrapeInterval` interval when
that flag is set, choosing the biggest timestamp and breaking ties on the biggest value.
Two consequences worth knowing before enabling it:

- **Set it below the fastest series you store**, not to a nominal scrape interval. Anything
  sampled faster than the interval is thinned, silently, with no error — data that simply
  looks sparser than it should.
- **A downward revision at the same timestamp is not honoured**, because the tie-break
  prefers the larger value. Buckets usually fill upward as ecobee completes them, so the
  tie-break normally picks the corrected value — but it is the one case where dedup and
  the revision handling in §5 pull against each other.

**Query cache.** VictoriaMetrics automatically resets its rollup result cache when samples
older than `-search.cacheTimestampOffset` (default 5m) are ingested. Every sample this
importer writes is older than that, so the reset is automatic — no `-search.disableCache`
and no manual flush.

Prometheus itself cannot ingest this format; supporting it means a remote-write backend.
The writer is an interface with that in mind, but it is **not implemented yet** (§10.1).

---

## 7. Observability of the importer itself

The `/metrics` endpoint carries **only facts about this process**. No house data.

| Metric | Meaning |
|---|---|
| `ecobee_reauth_required` | 1 = `invalid_grant`; a human must re-run bootstrap |
| `ecobee_last_successful_import_timestamp_seconds` | watermark of the last good cycle |
| `ecobee_newest_bucket_timestamp_seconds` | per thermostat; newest bucket ever seen |
| `ecobee_import_cycles_total` | by `result` (`success` / `error`) |
| `ecobee_api_requests_total` | by `endpoint`, `outcome` — the rate-discipline audit trail |
| `ecobee_samples_written_total` | volume actually landed |
| `ecobee_samples_skipped_total` | unchanged buckets suppressed rather than re-written (§5) |
| `ecobee_token_refreshes_total` | by `outcome`; rotation visible here |
| `ecobee_importer_build_info` | always 1; carries a `version` label |

Every one of these is a **per-process counter that resets when the pod restarts**. A low
`ecobee_samples_skipped_total` means a young process, not broken suppression — read it
alongside `ecobee_import_cycles_total`, since the first cycle after any start has an empty
cache and legitimately skips nothing.

**These are scraped, not pushed.** Alert rules over them use bare instant selectors. Do
**not** wrap them in `last_over_time()` — the process holds its gauges between cycles, so
the series never go stale, and widening the window would defeat the staleness rule that
`ecobee_last_successful_import_timestamp_seconds` exists to support.

The imported house metrics are the opposite: written in 15-minute batches, genuinely
stale between them, and any rule over *those* does need `last_over_time()`. Both kinds
live in this one workload, which is unusual and is why it is stated twice.

The alert that matters is `ecobee_reauth_required`. Everything else self-heals; that one
stops collection until a person with an authenticator app intervenes.

---

## 8. Rate discipline

Goal: never be the reason ecobee rate-limits or blocks an account.

| Call | Frequency | Per day |
|---|---|---|
| `GET /1/runtimeReport` | every 900s (documented floor) | 96 |
| `GET /1/thermostat` (metadata) | hourly | 24 |
| `POST /oauth/token` (refresh) | on expiry | ~24 |

**~144 requests per day, total, for the entire household.**

Enforced by construction, not by convention:

- `ECOBEE_IMPORT_INTERVAL_SECONDS` is clamped to a hard floor of 900. A smaller value logs
  a warning and is raised.
- All thermostats are fetched in one request rather than one request each.
- The loop never crashes, so restart storms cannot bypass the interval (§5).
- Every outbound call increments `ecobee_api_requests_total`, so the real rate is
  measurable rather than assumed.

---

## 9. Failure modes

| Failure | Detection | Behaviour | Recovery |
|---|---|---|---|
| Access token expired | HTTP **500**, `status.code 14` — not 401 | refresh, retry once | automatic |
| No access token loaded | empty bearer → `status.code` 1/16 | refresh before the first request (§4.4) | automatic |
| Refresh token rotated | new value returned | written back to Secret | automatic |
| `invalid_grant` | refresh rejected | `ecobee_reauth_required 1`, keep running, **re-read the store each cycle** | **human: replace the credential** — no restart needed |
| ecobee API 5xx / timeout | request fails | cycle counted as error, retried next cycle | automatic |
| Pod restart / eviction | — | re-import `STARTUP_LOOKBACK_HOURS` | automatic, no data lost |
| Cluster outage < lookback | — | next cycle backfills the gap | automatic |
| Outage > lookback | data gap in VM | — | one-off `--backfill-from` |
| VM unreachable | write fails | watermark **not** advanced; retried next cycle | automatic |
| ecobee changes Auth0 forms | bootstrap fails | existing tokens keep working | bump `python-ecobee-api` |
| Thermostat offline | its buckets stop | `ecobee_newest_bucket_timestamp_seconds` goes stale | investigate the thermostat |

The watermark advancing only after a successful write is what makes a destination outage
lossless rather than a silent hole.

Two of these rows are counter-intuitive enough to be worth restating: ecobee signals auth
failures with **HTTP 500 and a `status.code`**, not with 401 — a client that retries on 401
will never refresh — and a **rejected credential is recoverable without a restart**, because
the process re-reads its credential store on every such failure rather than holding the
rejected token for its lifetime.

---

## 10. Deliberate non-goals

### 10.1 Not built yet

- **Prometheus remote-write backend.** Specified in §6, unimplemented. Needed only for a
  Prometheus destination; VictoriaMetrics is served by the native import path.
- **Writes to ecobee.** This service only reads. Setpoint changes belong elsewhere.

### 10.2 `ecobee-mcp` as a co-tenant

Interactive "what is it doing right now" queries are served by
[ecobee-mcp](https://github.com/emrikol/ecobee-mcp), not by adding a live-poll path here.

If it is deployed, it **must** run in its `readonly` credential mode against the same
Secret — a mode built for precisely this, described upstream as piggybacking on another
application's authenticated session. The importer stays the sole token writer (§4.2) and
the MCP server only reads, which is what keeps the rotation race in §4.2 closed.

⚠️ Untested caveat: upstream's readonly mode re-reads its credentials **on a 401**. Given
ecobee reports auth failures as HTTP 500 with a `status.code` (§4.4), that trigger may
never fire, leaving it holding a rotated-away token until restarted. Worth verifying
before relying on it to recover unattended.

A second consideration for that deployment: `ecobee-mcp` ships write tools (set
temperature, set hold, create vacation, send message). The web OAuth scope includes
`smartWrite` and ecobee offers no read-only token, so unlike a credential-scoped
integration the boundary can only be enforced by the MCP server itself — by registering
read tools only. That is an access decision, not an importer concern, and it does not
block anything here.

### 10.3 Door and window SmartSensors — in scope after all

The widely repeated claim, including in this document's own first draft, is that ecobee's
915 MHz SmartSensors for doors and windows are invisible to the cloud API and reachable
only by pairing the thermostat to HomeKit Controller locally.

**That is wrong for `runtimeReport`.** The first live run against a real account returned
several as `sensorType: dryContact` — exterior and interior doors — with the same 5-minute
history as every other sensor. They are exported as
`ecobee_sensor_contact`, 1 open and 0 closed.

Two lessons worth keeping:

- The claim may well be true of the endpoints people usually check — the HomeKit workaround
  circulates because `GET /1/thermostat`'s `remoteSensors` does not carry them. "Absent from
  the API" was really "absent from the endpoint I looked at."
- Enumerating sensor types from the response metadata, rather than hardcoding the ones the
  documentation lists, is what made this work with no code change. The documented enum was
  not the thing to trust; the response was.
