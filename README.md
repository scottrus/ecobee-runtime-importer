# ecobee-runtime-importer

Imports ecobee's `runtimeReport` history into VictoriaMetrics: **5-minute
buckets** of zone temperature, humidity, setpoints, occupancy, outdoor
conditions, per-room sensor readings, and **equipment runtime in seconds** — the
duty-cycle measurement that live polling cannot reconstruct.

Works **without an ecobee developer API key**, which matters because ecobee no
longer issues them. It authenticates the way Home Assistant does as of 2026.3.

Design rationale, failure modes and the traps in ecobee's data are in
[ARCHITECTURE.md](./ARCHITECTURE.md). Read §3.2 before touching `transform.py`.

<!-- The leading ./ is required, not stylistic. `.md` is Moldova's ccTLD, so a
     bare `ARCHITECTURE.md` link is resolved as a HOSTNAME by browsers and by
     markdown previewers that hand the target to a URL bar — it navigates to the
     live website architecture.md instead of this repo's file. -->


---

## What you get

| Metric | |
|---|---|
| `ecobee_zone_temperature_fahrenheit` | per thermostat |
| `ecobee_zone_humidity_percent` | per thermostat (**not** per room — see below) |
| `ecobee_zone_heat_setpoint_fahrenheit` / `..._cool_...` | per thermostat |
| `ecobee_zone_occupancy` | per thermostat |
| `ecobee_outdoor_temperature_fahrenheit` / `..._humidity_percent` | per thermostat |
| `ecobee_equipment_runtime_seconds{equipment}` | seconds per 5-min bucket |
| `ecobee_zone_climate_info{climate}` / `ecobee_zone_hvac_mode_info{hvac_mode}` | value 1 |
| `ecobee_sensor_temperature_fahrenheit{sensor}` | per remote sensor |
| `ecobee_sensor_occupancy{sensor}` | per remote sensor |
| `ecobee_sensor_humidity_percent{sensor}` | the thermostats' own humidity sensors |
| `ecobee_sensor_contact{sensor}` | **door/window sensors** — 1 open, 0 closed |
| `ecobee_sensor_value{sensor,sensor_type}` | fallback for any type not listed above |

Duty cycle is `ecobee_equipment_runtime_seconds / 300`. It is a gauge of
seconds-per-bucket, **not** a counter — do not wrap it in `rate()`.

**Door and window SmartSensors are included.** They arrive as
`sensorType: dryContact` in the sensor report, with the same 5-minute history as
everything else. This contradicts the widely repeated claim that they are
invisible to the ecobee API — that claim is true of `GET /1/thermostat`, which is
where people look, but not of `runtimeReport`.

**There is no per-room humidity.** Room SmartSensors measure temperature and
occupancy; humidity is measured only at the thermostats themselves. Room-level
dewpoint analysis is limited to those locations.

---

## Setup

Four steps: clone, bootstrap a token, create the namespace and Secret, deploy.

```bash
git clone https://github.com/scottrus/ecobee-runtime-importer.git && cd ecobee-runtime-importer
```

### 1. Get a refresh token (once, interactively)

Needs your ecobee login and, if you have TOTP MFA enabled, your authenticator
app. This step cannot be automated — that is why it is a script you run rather
than a job that runs.

```bash
uv venv --allow-existing && uv pip install -e '.[bootstrap,dev]'
```

```bash
.venv/bin/python scripts/bootstrap.py --out ./credentials.json
```

It prompts for your email, then your password (not echoed), then a 6-digit code
if your account has TOTP MFA. Push, SMS and email MFA are not supported — only
authenticator-app codes.

`--out` writes the token to a 0600 file rather than printing it, keeping it out
of your terminal scrollback. Omit it to print instead. `credentials.json` is
gitignored.

Your password is used only to complete this login. It is never stored and never
reaches the running importer.

Put the `refresh_token` in your password manager, then read it back out for the
next step:

```bash
.venv/bin/python -c 'import json;print(json.load(open("credentials.json"))["refresh_token"])'
```

### 2. Create the namespace and Secret

The Secret has to exist before the Deployment starts, and it is created
out-of-band rather than applied from the repo — the importer rotates it in
place, so a committed copy would go stale immediately.

```bash
kubectl apply -f deploy/namespace.yaml
```

```bash
kubectl create secret generic ecobee-importer-tokens -n ecobee-runtime-importer --from-literal=refresh_token='PASTE_HERE'
```

> **This Secret becomes mutable state.** Auth0 may rotate the refresh token, and
> the importer patches the new value back into this Secret. After the first
> rotation the cluster's copy is authoritative. **Re-applying the vault copy
> later will lock the account out** and force you back to step 1. Back it up by
> reading it out of the cluster.

### 3. Deploy

Nothing to build. `deploy/` pins a published image, and every tagged release
pushes a multi-arch build to GHCR with an SBOM and a signed provenance
attestation.

Check `ECOBEE_VM_IMPORT_URL` in `deploy/configmap.yaml` first — it defaults to a
single-node VictoriaMetrics. For a cluster install use
`http://vminsert:8480/insert/0/prometheus/api/v1/import/prometheus`.

```bash
kubectl apply -k deploy/
```

### 4. Confirm it works

```bash
kubectl logs -n ecobee-runtime-importer deploy/ecobee-runtime-importer
```

Expect the thermostat list with time zones, then `Imported N samples`. First run
imports the last 24 hours, so `N` will be in the thousands. Then in VictoriaMetrics:

```
count_over_time(ecobee_zone_temperature_fahrenheit[24h])
```

---

## Development

Every CI check is a `make` target, so `make check` locally is the same gate a
pull request faces — there is no second copy of the commands to drift.

```bash
make check
```

Targets that need a tool you do not have are skipped with a `SKIP:` line. CI
sets `REQUIRE_ALL=1`, which turns each skip into a failure, so a check can never
appear to have run when it did not. `make help` lists everything.

```bash
uv venv --allow-existing && uv pip install -e '.[bootstrap,dev]'
```

```bash
.venv/bin/pytest
```

### Releasing

Releases are cut from `main` and gated on CI having passed for that exact
commit. Three things must agree, and the release workflow refuses the tag if
they do not:

| | |
|---|---|
| the git tag | `v0.1.0` |
| `__version__` in `src/ecobee_importer/__init__.py` | `0.1.0` |
| the image tag in `deploy/deployment.yaml` | `0.1.0` |

That third one is what keeps step 3 of the setup honest: a fresh clone applies
`deploy/` against an image that this release actually published. `make manifests`
checks the same agreement locally.

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow then builds `linux/amd64,linux/arm64`, pushes to GHCR with an SBOM
and `provenance: mode=max`, attests the build, and opens a GitHub release.

To run the importer against a local VictoriaMetrics, write
`{"refresh_token": "..."}` to `./credentials.json` from the bootstrap output,
then:

```bash
ECOBEE_TOKEN_STORE=file ECOBEE_TOKEN_FILE=./credentials.json ECOBEE_VM_IMPORT_URL=http://localhost:8428/api/v1/import/prometheus .venv/bin/python -m ecobee_importer
```

---

## Configuration

Everything is environment-driven, and in Kubernetes everything lives in
`deploy/configmap.yaml`, consumed with `envFrom`. Adapting to a different shape
is a ConfigMap edit — the Deployment does not need touching, and a key added
there needs no matching entry in the pod spec.

Environment is read at process start, so after editing:

```bash
kubectl rollout restart deploy/ecobee-runtime-importer -n ecobee-runtime-importer
```

That is cheap: there is no persisted state, and the startup lookback re-imports
recent history idempotently.

| Variable | Default | Notes |
|---|---|---|
| `ECOBEE_TOKEN_STORE` | `file` | `file` or `kubernetes` |
| `ECOBEE_TOKEN_FILE` | `/var/lib/ecobee/credentials.json` | `file` store only |
| `ECOBEE_SECRET_NAME` | `ecobee-importer-tokens` | **also in `rbac.yaml`** — see below |
| `ECOBEE_SECRET_NAMESPACE` | pod's own namespace | Set only to read a Secret elsewhere |
| `ECOBEE_VM_IMPORT_URL` | `http://victoriametrics:8428/api/v1/import/prometheus` | |
| `ECOBEE_VM_AUTH_HEADER_FILE` | — | File whose contents become the `Authorization` header |
| `ECOBEE_WRITE_TIMEOUT_SECONDS` | `60` | |
| `ECOBEE_IMPORT_INTERVAL_SECONDS` | `900` | **Hard floor 900.** Lower values are clamped |
| `ECOBEE_STARTUP_LOOKBACK_HOURS` | `24` | Re-imported on every restart; capped at 31 days |
| `ECOBEE_OVERLAP_MINUTES` | `60` | Re-request recent buckets to pick up late data |
| `ECOBEE_THERMOSTAT_CACHE_SECONDS` | `3600` | How often names/time zones are refreshed |
| `ECOBEE_INCLUDE_SENSORS` | `true` | Per-remote-sensor history |
| `ECOBEE_EXTRA_COLUMNS` | — | CSV. **Adds to** the default set |
| `ECOBEE_COLUMNS` | — | CSV. **Replaces** the default set |
| `ECOBEE_EXTRA_LABELS` | — | `key=value,...` on every sample |
| `ECOBEE_METRICS_PORT` | `9863` | Must match `containerPort` in the Deployment |
| `ECOBEE_LOG_LEVEL` | `INFO` | |

### Two values that are coupled outside the ConfigMap

**`ECOBEE_SECRET_NAME` ↔ `rbac.yaml`.** The Role is scoped to a single Secret by
`resourceNames`, which is what keeps `patch` on secrets from meaning *every*
secret in the namespace. Renaming the Secret in the ConfigMap alone gives a 403;
the importer catches that specific case and logs which file to change.

**`ECOBEE_METRICS_PORT` ↔ `containerPort`.** Changing it in one place leaves the
Service pointing at nothing.

### Other shapes

- **Cluster VictoriaMetrics** — point `ECOBEE_VM_IMPORT_URL` at
  `http://vminsert:8480/insert/0/prometheus/api/v1/import/prometheus`.
- **Authenticated destination** — mount a Secret and set
  `ECOBEE_VM_AUTH_HEADER_FILE` to the mounted path. It is a *path*, not a value,
  so the credential never enters the ConfigMap, and it is re-read per write so
  rotation needs no restart.
- **A different namespace** — change `namespace:` in `kustomization.yaml` and the
  name in `namespace.yaml`. Nothing else needs editing; the Secret's namespace
  resolves from the pod's own ServiceAccount mount. Note the CRs are only
  discovered from a non-monitoring namespace if your VMAgent and VMAlert run
  `selectAllByDefault: true` (or you add matching namespace selectors).
- **Default-deny egress** — this namespace is new, so it has no NetworkPolicy of
  its own. If your cluster default-denies, the importer needs egress to
  `api.ecobee.com`, `auth.ecobee.com`, the Kubernetes API, and your metrics
  destination.
- **More than one ecobee account** — a second instance with its own ConfigMap,
  Secret, and `ECOBEE_EXTRA_LABELS=site=...` to keep the series apart.

---

## Not abusing the API

ecobee's documentation is explicit: *"DO NOT request report data at an interval
quicker than once every 15 minutes."*

Steady state, for the whole household:

| Call | Frequency | Per day |
|---|---|---|
| `runtimeReport` (all thermostats, one request) | 900s | 96 |
| `thermostat` (names, time zones) | hourly | 24 |
| token refresh | on expiry | ~24 |

**~144 requests/day.** The interval is clamped in code, all thermostats share
one request, and the loop never crashes — so a restart storm cannot bypass the
interval. `ecobee_api_requests_total` is the audit trail if you want to check
rather than trust.

---

## Recovering a gap

Outages shorter than `ECOBEE_STARTUP_LOOKBACK_HOURS` heal themselves on the next
cycle; nothing to do. Longer ones, up to the API's 31-day limit:

```bash
kubectl exec -n ecobee-runtime-importer deploy/ecobee-runtime-importer -- ecobee-runtime-importer --backfill-from 2026-08-01
```

Re-importing data you already have is harmless — same timestamp, same value.

---

## Troubleshooting

**`ecobee_reauth_required 1`** — the refresh token is dead. Re-run step 1 and
update the Secret. No history is lost; the report serves 31 days retroactively,
so the next cycle plus a `--backfill-from` recovers the gap.

**Everything is off by several hours** — a time zone bug. Report rows arrive in
*thermostat local time*, not UTC (ARCHITECTURE.md §3.2). Check the thermostat's
`location.timeZone` in the startup log.

**Temperatures read like `7.37`** — something reintroduced a ÷10. runtimeReport
returns decimal degrees (`73.7`) and must not be scaled; beestat's `/10`, the
usual reference, does not apply to this endpoint.

**A column is missing from VictoriaMetrics** — unmapped columns are skipped with
an `INFO` log rather than exported with a guessed unit. Add it to `transform.py`
once you have verified what its units actually are.

**Login fails during bootstrap** — ecobee may have changed its Auth0 forms.
Upgrade `python-ecobee-api`; Home Assistant depends on the same library, so
fixes land upstream quickly. Already-issued tokens keep working meanwhile.

---

## License

Apache-2.0. See [LICENSE](./LICENSE).
