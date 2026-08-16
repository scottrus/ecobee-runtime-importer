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
make bootstrap
```

This builds the venv first (`uv` if installed, stdlib `venv` otherwise), so it
works on a machine with neither. On Debian and Ubuntu the fallback needs
`python3-venv` (`apt install python3-venv`) if `python3 -m venv` reports that
`ensurepip` is unavailable.

It prompts for your email, then your password (not echoed), then a 6-digit code
if your account has TOTP MFA. Push, SMS and email MFA are not supported — only
authenticator-app codes.

It writes `./credentials.json` at mode 0600 and stops there. The token stays out
of your scrollback, and the file is gitignored. (`--out PATH` moves it;
`--print` puts it on stdout instead, which you rarely want.)

Your password is used only to complete this login. It is never stored and never
reaches the running importer.

Put the `refresh_token` in your password manager — this is your only copy until
the Secret exists:

```bash
.venv/bin/python -c 'import json;print(json.load(open("credentials.json"))["refresh_token"])'
```

### 2. Create the namespace and Secret

```bash
make secret
```

That creates the namespace, then creates the Secret by reading the token straight
out of the file bootstrap wrote. It is safe to re-run: it **replaces** an
existing Secret rather than failing, so the same command serves first install and
recovery. The Secret is created out-of-band rather than applied from the repo
because the importer rotates it in place, so a committed copy would go stale
immediately.

Then delete the local copy — the cluster's is authoritative from here:

```bash
rm credentials.json
```

> **This Secret becomes mutable state.** Auth0 may rotate the refresh token, and
> the importer patches the new value back into this Secret. After the first
> rotation the cluster's copy is authoritative. **Re-applying the vault copy
> later will lock the account out** and force you back to step 1. Back it up by
> reading it out of the cluster.

### 3. Deploy

Nothing to build. Every tagged release publishes a multi-arch image with an SBOM
and a signed provenance attestation, plus a versioned Helm chart to GHCR.

```bash
make deploy
```

That is `helm upgrade --install` with the chart in this repo. To install a
published chart version instead, without a clone:

```bash
helm upgrade --install ecobee-runtime-importer oci://ghcr.io/scottrus/charts/ecobee-runtime-importer --version 0.1.4 --namespace ecobee-runtime-importer --create-namespace --set fullnameOverride=ecobee-runtime-importer
```

**Set `victoriaMetrics.url` for your cluster.** The default is a placeholder, and
a bare service name will not resolve from another namespace:

```bash
make deploy HELM_ARGS='--set victoriaMetrics.url=http://vmsingle-vmks-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428/api/v1/import/prometheus'
```

Or keep a values file and pass `-f`. See [Configuration](#configuration).

> **Keep `fullnameOverride`.** Without it Helm prefixes the release name onto
> every resource, which changes the `job` label and orphans existing metric
> history under the old name.

### 4. Confirm it works

```bash
kubectl logs -n ecobee-runtime-importer deploy/ecobee-runtime-importer -f
```

A healthy first cycle looks like this:

```
Loaded credentials: Tokens(refresh=set, access=empty, api_key=unset)
No access token loaded; refreshing before first request
Persisted rotated tokens to Secret ...
Thermostats: Upstairs (America/New_York), Main Floor (...), Basement (...), Suite (...)
Fetching runtimeReport ... for 4 thermostat(s)
Wrote 30712 samples to http://.../api/v1/import/prometheus
Imported 30712 samples, newest bucket ...
```

`access=empty` on the first line is expected: the Secret holds only the refresh
token, so the first act is a refresh, and the rotated pair is written straight
back. The startup lookback pulls 24 hours, so the first count is in the tens of
thousands; later cycles are a few hundred.

Then in VictoriaMetrics:

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
make setup && .venv/bin/pytest
```

### Releasing

**No pull request declares a version.** The git tag is the only place a version
exists:

- the **package** version comes from `setuptools-scm`, which reads the tag —
  local trees get `0.1.5.dev3+g9e3b566`, release builds get the tag exactly;
- the **chart** version and `appVersion` are injected at package time
  (`helm package --version "$TAG" --app-version "$TAG"`), so `Chart.yaml` holds
  `0.0.0` placeholders forever;
- the **image tag** follows `appVersion`.

So a fix PR contains only the fix. Releasing is:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

The workflow refuses the tag unless that exact commit is on `main` **and** CI
passed on it — branch protection is what stands behind every release. It then
builds `linux/amd64,linux/arm64`, publishes the image and chart to GHCR, attests
the build, and opens a GitHub release.

Wait for CI to go green on `main` before tagging: the gate cannot tell "CI is
still running" from "CI failed", so tagging too early fails the release. Re-run
it once CI finishes — no re-tag needed, since the commit has not changed.

## Configuration

Everything lives in the chart's `values.yaml`. Nothing has to agree with
anything else by hand — the values that used to be duplicated are now rendered
from one place each:

| Value | Drives |
|---|---|
| `credentials.existingSecret` | the env var **and** the Role's `resourceNames` |
| `service.port` | `containerPort`, the Service, **and** `ECOBEE_METRICS_PORT` |
| `.Release.Namespace` | every resource, via `--namespace` |
| `.Chart.AppVersion` | the image tag, injected from the git tag at release |

A values change rolls the pods automatically: the pod template carries a
`checksum/config` annotation over the rendered ConfigMap, so an edit changes the
pod spec. That matters because the process reads its environment once at startup.

| Key | Default | Notes |
|---|---|---|
| `victoriaMetrics.url` | placeholder | **Set this.** Needs an FQDN cross-namespace |
| `victoriaMetrics.authHeaderFile` | — | Path to a mounted file; never put the value in values |
| `credentials.existingSecret` | `ecobee-importer-tokens` | Created out of band; never templated |
| `credentials.namespace` | — | Only to read a Secret elsewhere; needs a ClusterRole |
| `importIntervalSeconds` | `900` | **Hard floor 900.** Lower values are clamped |
| `startupLookbackHours` | `24` | Re-imported on every restart; capped at 31 days |
| `overlapMinutes` | `60` | Re-request recent buckets to pick up late data |
| `collection.includeSensors` | `true` | Per-remote-sensor history |
| `collection.extraColumns` / `.columns` | — | Add to / replace the default column set |
| `collection.extraLabels` | `{}` | Static labels on every sample |
| `service.port` | `9863` | |
| `vmServiceScrape.enabled` / `vmRule.enabled` | `true` | Set false without the VM operator |
| `logLevel` | `INFO` | |

### Deploying to a different namespace

```bash
make deploy NAMESPACE=my-namespace
```

Helm namespaces the whole release, so nothing in the chart needs editing. The
importer resolves its Secret's namespace from the pod at runtime.

One caveat outside this repo's control: `VMServiceScrape` and `VMRule` are only
discovered from an arbitrary namespace if your VMAgent and VMAlert run
`selectAllByDefault: true`, or you add matching namespace selectors.

### The Secret is never templated

The chart will not create the credential Secret, and `make helm` fails if a
template ever renders one. The importer **writes** that Secret — Auth0 rotates
the refresh token and the new value is patched back — so a Helm-managed copy
would be reset on every `helm upgrade`, presenting a revoked token and requiring
a fresh interactive login.

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

## Re-authenticating

When `ecobee_reauth_required` is 1 — or the log says *"Re-authentication
required"* — the refresh token has been rejected by ecobee and no amount of
waiting fixes it. This is the one failure that needs a human with an
authenticator app.

```bash
make reauth
```

That is `bootstrap` + `secret` + `restart`: log in, replace the Secret, roll the
pod. Then clean up and watch it recover:

```bash
rm credentials.json && kubectl logs -n ecobee-runtime-importer deploy/ecobee-runtime-importer -f
```

The three steps are each available on their own:

- `make secret` **replaces** an existing Secret. Plain `kubectl create secret`
  fails once one exists, so the setup command is the wrong one for recovery.
- `make restart` is **no longer required** — the importer re-reads the
  credential store after a rejected token, so correcting the Secret is enough on
  its own and it recovers within one cycle. `make reauth` still restarts,
  because waiting up to 15 minutes to find out whether you fixed it is a poor
  way to spend an incident.

The log says which case you are in:

```
Credential store now holds a different token; retrying on the next cycle.
```

```
The credential store still holds the token that was just rejected.
```

The second means the Secret was never actually updated — check that `make
secret` ran against the namespace the importer is deployed in.

**No history is lost.** `runtimeReport` serves up to 31 days retroactively, so
the startup lookback plus a `--backfill-from` recovers everything the outage
covered.

> **Each login may invalidate the previous one.** If ecobee's Auth0 tenant
> issues one refresh token per user per client, re-running bootstrap anywhere
> kills the token your cluster is using. Mint once, put it straight into the
> Secret, and don't run bootstrap on a second machine "to check" — that is
> itself a way to cause this failure.

---

## Troubleshooting

**`ecobee_reauth_required 1`** — see [Re-authenticating](#re-authenticating)
above.

**Timeout writing to VictoriaMetrics, while ecobee calls succeed** — the
destination's network policy does not list the importer as a client. A
`CiliumNetworkPolicy` or `NetworkPolicy` that selects the metrics backend denies
everything not explicitly allowed, and a denied connection **times out** rather
than being refused, which reads like DNS or a wrong URL. Cross-namespace rules
need the namespace label explicitly — a bare `matchLabels` matches only the
policy's own namespace. Nothing is lost meanwhile: the watermark advances only
after a successful write, so the backlog imports once the policy is fixed.

**`ZoneInfoNotFoundError` / `No module named 'tzdata'`** — the image has no tz
database. `tzdata` is a hard dependency for this reason; if you built your own
image, keep it.

**Everything is off by several hours** — a time zone bug. Report rows arrive in
*thermostat local time*, not UTC (ARCHITECTURE.md §3.2). Check the thermostat's
`location.timeZone` in the startup log. Note the importer now **fails** rather
than falling back to UTC, precisely so this cannot happen silently.

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
