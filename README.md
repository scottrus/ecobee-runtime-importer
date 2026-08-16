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

Nothing to build. `deploy/` pins a published image, and every tagged release
pushes a multi-arch build to GHCR with an SBOM and a signed provenance
attestation.

**Check `ECOBEE_VM_IMPORT_URL` in `deploy/config.env` first.** The importer runs
in its own namespace, so a bare service name will not resolve — the destination
needs a fully qualified name:

```
http://vmsingle-<release>.<namespace>.svc.cluster.local:8428/api/v1/import/prometheus
```

```bash
make deploy
```

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

Releases are cut from `main` and gated on CI having passed for that exact
commit. Three things must agree, and the release workflow refuses the tag if
they do not:

| | |
|---|---|
| the git tag | `vX.Y.Z` |
| `__version__` in `src/ecobee_importer/__init__.py` | `X.Y.Z` |
| the image tag in `deploy/deployment.yaml` | `X.Y.Z` |

That third one is what keeps step 3 of the setup honest: a fresh clone applies
`deploy/` against an image that this release actually published. `make manifests`
checks the same agreement locally.

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
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
`deploy/config.env`, rendered into a ConfigMap by kustomize and consumed with
`envFrom`. Adapting to a different shape is a one-file edit — the Deployment
needs no changes, and a key added there needs no matching entry in the pod spec.

**A config edit rolls the pods by itself.** The ConfigMap is generated, so its
name carries a hash of the contents; changing `config.env` changes the pod spec,
and `make deploy` restarts the deployment as a result. That matters because the
process reads its environment once at startup — with a hand-written ConfigMap, an
edit applies cleanly, changes nothing, and waits silently for an unrelated
restart.

```bash
make deploy
```

Restarting is cheap: there is no persisted state, and the startup lookback
re-imports recent history idempotently.

| Variable | Default | Notes |
|---|---|---|
| `ECOBEE_TOKEN_STORE` | `file` | `file` or `kubernetes` |
| `ECOBEE_TOKEN_FILE` | `/var/lib/ecobee/credentials.json` | `file` store only |
| `ECOBEE_SECRET_NAME` | `ecobee-importer-tokens` | kustomize copies it into the Role's `resourceNames` |
| `ECOBEE_SECRET_NAMESPACE` | pod's own namespace | Set only to read a Secret elsewhere |
| `ECOBEE_VM_IMPORT_URL` | `http://victoriametrics:8428/...` | Code default only; a bare name will not resolve cross-namespace. `config.env` sets a real FQDN |
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
| `ECOBEE_METRICS_PORT` | `9863` | Not set in `config.env` — see below |
| `ECOBEE_LOG_LEVEL` | `INFO` | |

### Where each value is defined once

Three things used to be "must match" rules kept by comments. They are now
mechanical:

**The Secret name** lives only in `config.env`. A kustomize `replacement:` copies
it into the Role's `resourceNames`, which is what keeps `patch` on secrets from
meaning *every* secret in the namespace. Renaming it in one place is now the only
way to rename it.

**The namespace** lives only in the `namespace:` field of `kustomization.yaml`.
The transformer rewrites every resource including the `Namespace` object, and the
Makefile reads the same field.

**The metrics port** lives only in `containerPort`. `ECOBEE_METRICS_PORT` is
deliberately absent from `config.env` — the application default is the same
number, and the Service targets the port *name*, so nothing can disagree. Set it
only if you also change `containerPort`.

### Other shapes

- **Cluster VictoriaMetrics** — point `ECOBEE_VM_IMPORT_URL` at
  `http://vminsert.<ns>.svc.cluster.local:8480/insert/0/prometheus/api/v1/import/prometheus`.
- **Authenticated destination** — mount a Secret and set
  `ECOBEE_VM_AUTH_HEADER_FILE` to the mounted path. It is a *path*, not a value,
  so the credential never enters the ConfigMap, and it is re-read per write so
  rotation needs no restart.
- **A different namespace** — change **one line**: `namespace:` in
  `deploy/kustomization.yaml`. See below.
- **Network policy** — if your cluster restricts traffic, both directions matter.
  See the troubleshooting entry on write timeouts.

### Deploying to a different namespace

Edit the `namespace:` field in `deploy/kustomization.yaml`. That is the only
change required:

```yaml
namespace: my-namespace
```

kustomize's transformer rewrites `metadata.namespace` on every namespaced
resource **and renames the `Namespace` resource itself**, the Makefile reads that
same field for its `kubectl` targets, and the importer resolves its Secret's
namespace from the pod's own ServiceAccount mount at runtime. Nothing else — not
`namespace.yaml`, not `rbac.yaml`, not the Makefile — needs editing.

Check the result before applying:

```bash
kubectl kustomize deploy/ | grep -E '^kind:|namespace:'
```

One caveat outside this repo's control: `VMServiceScrape` and `VMRule` are only
discovered from an arbitrary namespace if your VMAgent and VMAlert run
`selectAllByDefault: true`, or you add matching namespace selectors.
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
