# syntax=docker/dockerfile:1

# Chainguard images are used for their near-zero CVE count. The free public tier
# publishes only `latest` and `latest-dev` — there are no version tags — so both
# stages are pinned by digest instead. Dependabot updates these.
#
# Re-resolve a digest by hand with:
#   crane digest cgr.dev/chainguard/python:latest

# --- build ------------------------------------------------------------------
# The -dev variant carries pip and a shell; the runtime variant carries neither.
FROM cgr.dev/chainguard/python:latest-dev@sha256:3472bbd8c7a7fe9254dcccdbd8eefb8edf603d0882aa4ed527f8497957b10f5f AS build

# Chainguard's -dev variants still default to the nonroot user, so writing to /
# is denied. Switch to root for the build only — this stage is discarded, and the
# runtime stage below runs as 65532 regardless.
USER root

WORKDIR /src

# Build into a venv so the runtime stage copies exactly one self-contained
# directory. The runtime image has no pip to install with.
#
# The venv path must be identical in both stages: a venv bakes its own absolute
# path into the console-script shebangs, so copying it elsewhere silently
# produces entrypoints that point at a python that isn't there.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# setuptools-scm derives the version from git, and the build context has no .git
# — copying it in would bloat every layer and still break for a tarball build.
# The release workflow passes the tag; local builds get a placeholder, which is
# why a locally built image reports 0.0.0 rather than lying about a release.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

RUN pip install --no-cache-dir .

# Drop back to nonroot so this stage does not end as root. The stage is discarded
# either way, but leaving it root trips hadolint's DL3002 — and suppressing that
# lint here would also suppress it for the runtime stage, where it matters.
USER nonroot

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:27c8d0e215a516e6d366549d1673baf6c42784277afcc8d904715a73982b9ba4

LABEL org.opencontainers.image.title="ecobee-runtime-importer" \
      org.opencontainers.image.description="Imports ecobee runtimeReport history into VictoriaMetrics, without a developer API key" \
      org.opencontainers.image.source="https://github.com/scottrus/ecobee-runtime-importer" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/python:latest"

COPY --from=build /venv /venv

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Chainguard runtime images already default to the nonroot user (65532), which
# matches runAsUser in deploy/deployment.yaml. Stated explicitly so it survives
# a base image change.
USER 65532:65532

EXPOSE 9863

# Exec form keeps the importer as PID 1, so SIGTERM reaches it directly and both
# `docker stop` and Kubernetes termination are clean — the loop finishes its
# current cycle and exits rather than being killed mid-write.
ENTRYPOINT ["ecobee-runtime-importer"]

# No shell in this image, so the check is an exec-form python one-liner.
#
# /metrics is the only endpoint served, and it answers as soon as the process is
# up — deliberately, it does NOT reflect whether ecobee is reachable. A failed
# import is a metric and an alert, not an unhealthy container: restarting the
# pod would fix nothing and would re-request the whole lookback window on boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9863/metrics', timeout=4).status==200 else 1)"]
