# Every check that runs in CI is defined here and nowhere else.
#
# The CI workflow invokes these same targets, so `make check` locally is the same
# gate a pull request faces -- there is no second copy of the commands to drift.
#
# Tools that are not installed are skipped with a warning, so this is useful on a
# laptop without Docker. CI sets REQUIRE_ALL=1, which turns every skip into a
# failure, so a missing tool can never quietly pass in CI.

SHELL := /bin/sh

VENV   ?= .venv
PY     ?= $(VENV)/bin/python
PIP    ?= $(VENV)/bin/pip
IMAGE  ?= ecobee-runtime-importer:dev
CHART  ?= charts/ecobee-runtime-importer
RELEASE   ?= ecobee-runtime-importer

# Derived by setuptools-scm from the git tag, read back out of the installed
# package. No version literal exists in the tree to scrape.
VERSION = $(shell $(PY) -c 'import ecobee_importer; print(ecobee_importer.__version__)' 2>/dev/null)

# Read from the single place each value is defined, so these targets cannot point
# somewhere the manifests do not.
#
# Both sources are read as flat text on purpose. An earlier version scraped
# YAML with sed, which is brittle enough that it needed a guard for the case
# where the pattern silently matched nothing:
#   - the namespace is the `namespace:` field of kustomization.yaml, which is a
#     single unambiguous line and the authority for the whole deployment;
#   - the Secret name is a KEY=VALUE line in config.env, no YAML involved.
NAMESPACE ?= ecobee-runtime-importer
SECRET    ?= $(shell $(HELM_BIN) show values $(CHART) 2>/dev/null | awk '/^  existingSecret:/{print $$2; exit}')
HELM_BIN  ?= helm
CREDS     ?= ./credentials.json

.DEFAULT_GOAL := help

define missing
if [ -n "$(REQUIRE_ALL)" ]; then \
  echo "ERROR: $(1) is required but not installed" >&2; exit 1; \
else echo "SKIP: $(2) ($(1) not installed)"; fi
endef

.PHONY: help
help:
	@echo "Local validation -- mirrors the PR checks exactly."
	@echo
	@echo "  make setup           create $(VENV) and install the package + dev deps"
	@echo "  make check           run everything below; the full PR gate"
	@echo
	@echo "Install and recovery (needs kubectl):"
	@echo "  make bootstrap       log in to ecobee, write $(CREDS)"
	@echo "  make secret          create OR replace the Secret (applies the namespace)"
	@echo "  make deploy          kubectl apply -k $(DEPLOY)/"
	@echo "  make restart         roll the deployment (needed after a Secret change)"
	@echo "  make reauth          bootstrap + secret + restart, for a dead token"
	@echo
	@echo "  make lint            ruff check, ruff format --check"
	@echo "  make fmt             apply ruff formatting and autofixes"
	@echo "  make test            pytest"
	@echo "  make actionlint      validate workflow syntax, expressions, run: blocks"
	@echo "  make actions-pinned  every uses: is SHA-pinned with a version comment"
	@echo "  make helm            helm lint, template permutations, kubeconform"
	@echo "  make docker          hadolint, image build, smoke test"
	@echo "  make scan            grype CVE scan (run 'make docker' first)"
	@echo
	@echo "  REQUIRE_ALL=1        turn 'tool not installed' skips into failures"

# uv when it is available, stdlib venv otherwise. This matters rather than being
# a nicety: `uv venv` does NOT install pip into the environment, so a Makefile
# that shells out to $(VENV)/bin/pip breaks on exactly the venv the README tells
# people to create. CI has no uv and takes the pip path.
UV := $(shell command -v uv 2>/dev/null || echo "")

# The venv is validated by probing for a usable installer, NOT by the directory
# existing. A failed `python3 -m venv` (no ensurepip, the default on Debian and
# Ubuntu) still leaves .venv/ behind, and a directory-timestamp target would then
# be considered satisfied forever -- every later run skips creation and fails on a
# missing .venv/bin/pip, which looks nothing like the original error.
.PHONY: venv
venv:
	@if [ -n "$(UV)" ]; then \
		uv venv --allow-existing $(VENV); \
	elif [ ! -x "$(PIP)" ]; then \
		rm -rf $(VENV); \
		python3 -m venv $(VENV) || { \
			rm -rf $(VENV); \
			echo "ERROR: python3 -m venv failed."; \
			echo "  Debian/Ubuntu ship python3 without ensurepip. Install the venv"; \
			echo "  package named in the message above -- 'apt install python3-venv',"; \
			echo "  or the version-specific 'apt install python3.12-venv'."; \
			exit 1; }; \
		$(PIP) install --quiet --upgrade pip; \
	fi

.PHONY: setup
setup: venv
	@if [ -n "$(UV)" ]; then uv pip install --quiet --python $(PY) -e ".[bootstrap,dev]"; \
	else $(PIP) install --quiet -e ".[bootstrap,dev]"; fi

# ------------------------------------------------------------ credentials ----
#
# The one interactive step in this project, plus the two commands that follow it.
# They are here because getting them slightly wrong is easy and the failure is
# expensive: `kubectl create secret` errors when the Secret already exists, and a
# Secret updated under a running pod is ignored until that pod restarts.

.PHONY: bootstrap
bootstrap: setup
	@$(PY) scripts/bootstrap.py --out $(CREDS)

# Both names are scraped out of YAML, so a reformat could silently yield an empty
# string and send these commands at `-n ""`. That is the same shape as the bug
# that made the importer request secrets at cluster scope, so it is checked.
.PHONY: check-derived
check-derived:
	@test -n "$(NAMESPACE)" || { echo "ERROR: NAMESPACE is empty"; exit 1; }
	@test -n "$(SECRET)" || { \
		echo "ERROR: could not read credentials.existingSecret from $(CHART)/values.yaml"; exit 1; }

# Idempotent, and a dependency of `secret` on purpose: creating the Secret before
# the namespace exists fails with "namespaces not found", which is a trap the
# ordering of two hand-run commands should not be able to spring.
#
# `helm upgrade --install --create-namespace` would also make it, but the Secret
# has to exist BEFORE the first install or the pod crash-loops on a missing
# credential — so the namespace is created here, ahead of it.
.PHONY: namespace
namespace:
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

.PHONY: deploy
deploy: check-derived
	@$(HELM_BIN) upgrade --install $(RELEASE) $(CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set fullnameOverride=$(RELEASE) \
		$(HELM_ARGS)

.PHONY: secret
secret: check-derived namespace
	@test -f $(CREDS) || { \
		echo "ERROR: $(CREDS) not found. Run 'make bootstrap' first."; exit 1; }
	@echo "==> $(SECRET) in namespace $(NAMESPACE)"
	@kubectl create secret generic $(SECRET) -n $(NAMESPACE) \
		--from-literal=refresh_token="$$($(PY) -c \
			'import json,sys;print(json.load(open(sys.argv[1]))["refresh_token"])' \
			$(CREDS))" \
		--dry-run=client -o yaml | kubectl apply -f -
	@echo "    the cluster's copy is authoritative now; remove the local one:"
	@echo "      rm $(CREDS)"

.PHONY: restart
restart: check-derived
	@kubectl rollout restart deploy/$(RELEASE) -n $(NAMESPACE)

# Full recovery from ecobee_reauth_required, in one step.
#
# The restart is no longer strictly required — the importer re-reads its
# credential store after a rejected token, so `make secret` alone recovers
# within one cycle. It stays in the chain because waiting up to 15 minutes to
# learn whether the fix worked is a poor way to spend an incident.
.PHONY: reauth
reauth: bootstrap secret restart

# ---------------------------------------------------------------- python ----

.PHONY: lint
lint: setup
	@echo "==> ruff check"
	@$(PY) -m ruff check --output-format=concise .
	@echo "==> ruff format --check"
	@$(PY) -m ruff format --check .

.PHONY: fmt
fmt: setup
	@$(PY) -m ruff format .
	@$(PY) -m ruff check --fix .

.PHONY: test
test: setup
	@echo "==> pytest"
	@$(PY) -m pytest -q

# ------------------------------------------------------------- workflows ----

.PHONY: actionlint
actionlint:
	@if ! command -v actionlint >/dev/null 2>&1; then $(call missing,actionlint,actionlint); \
		echo "     install with: brew install actionlint"; \
	else \
		echo "==> actionlint"; actionlint -color; echo "    workflows valid"; \
	fi

# Every `uses:` must be pinned to a 40-character commit SHA. A floating tag is
# mutable: the same workflow can run different code tomorrow.
.PHONY: actions-pinned
actions-pinned:
	@echo "==> action pinning"
	@unpinned=$$(grep -hoE "uses: +[^ ]+" .github/workflows/*.yml \
		| awk '{print $$2}' | grep -vE "@[0-9a-f]{40}$$" || true); \
	if [ -n "$$unpinned" ]; then \
		echo "FAIL: not pinned to a commit SHA:"; echo "$$unpinned" | sed 's/^/      /'; exit 1; \
	fi; \
	missing_comment=$$(grep -hE "uses: +[^ ]+@[0-9a-f]{40}" .github/workflows/*.yml \
		| grep -vE "# *v[0-9]" || true); \
	if [ -n "$$missing_comment" ]; then \
		echo "FAIL: pinned but missing a '# vX.Y.Z' comment:"; \
		echo "$$missing_comment" | sed 's/^ */      /'; exit 1; \
	fi; \
	echo "    all uses: are SHA-pinned with a version comment"

# ------------------------------------------------------------------ helm ----

.PHONY: helm
helm: helm-lint helm-template helm-schema

.PHONY: helm-lint
helm-lint:
	@if ! command -v $(HELM_BIN) >/dev/null 2>&1; then $(call missing,helm,helm lint); else \
		echo "==> helm lint"; $(HELM_BIN) lint $(CHART); \
	fi

# Permutations, not just a default render. Each assertion below is a property the
# deployment depends on and that a template edit could silently break.
.PHONY: helm-template
helm-template:
	@if ! command -v $(HELM_BIN) >/dev/null 2>&1; then $(call missing,helm,helm template); else \
		set -e; echo "==> helm template permutations"; \
		$(HELM_BIN) template t $(CHART) > /tmp/eri-default.yaml; \
		if grep -q "^kind: Secret" /tmp/eri-default.yaml; then \
			echo "FAIL: the chart rendered a Secret."; \
			echo "      The importer ROTATES its credential Secret in place, so a"; \
			echo "      Helm-managed one would be reset on every upgrade, presenting"; \
			echo "      a revoked token and locking the account out."; exit 1; fi; \
		grep -q "replicas: 1" /tmp/eri-default.yaml \
			|| { echo "FAIL: replicas must be 1 — a second replica is a second token writer"; exit 1; }; \
		grep -q "type: Recreate" /tmp/eri-default.yaml \
			|| { echo "FAIL: strategy must be Recreate, so a rollout never runs two token writers"; exit 1; }; \
		grep -q "checksum/config" /tmp/eri-default.yaml \
			|| { echo "FAIL: no config checksum — a values change would not restart the pod"; exit 1; }; \
		secret=$$($(HELM_BIN) template t $(CHART) --set credentials.existingSecret=other-name); \
		echo "$$secret" | grep -q 'resourceNames: \["other-name"\]' \
			|| { echo "FAIL: the Role's resourceNames did not follow credentials.existingSecret"; exit 1; }; \
		echo "$$secret" | grep -q 'ECOBEE_SECRET_NAME: "other-name"' \
			|| { echo "FAIL: ECOBEE_SECRET_NAME did not follow credentials.existingSecret"; exit 1; }; \
		port=$$($(HELM_BIN) template t $(CHART) --set service.port=9999); \
		echo "$$port" | grep -q "containerPort: 9999" \
			|| { echo "FAIL: containerPort did not follow service.port"; exit 1; }; \
		echo "$$port" | grep -q 'ECOBEE_METRICS_PORT: "9999"' \
			|| { echo "FAIL: ECOBEE_METRICS_PORT did not follow service.port"; exit 1; }; \
		$(HELM_BIN) template t $(CHART) --set rbac.create=false | grep -q "kind: Role" \
			&& { echo "FAIL: rbac.create=false still rendered a Role"; exit 1; } || true; \
		echo "    all permutations rendered as expected"; \
	fi

.PHONY: helm-schema
helm-schema:
	@if ! command -v $(HELM_BIN) >/dev/null 2>&1; then $(call missing,helm,kubeconform); \
	elif ! command -v kubeconform >/dev/null 2>&1; then \
		$(call missing,kubeconform,kubeconform); \
		echo "     install with: brew install kubeconform"; \
	else \
		echo "==> kubeconform"; \
		$(HELM_BIN) template t $(CHART) \
			| kubeconform -strict -summary -schema-location default \
				-skip VMServiceScrape,VMRule,ServiceMonitor; \
	fi

# ---------------------------------------------------------------- docker ----

.PHONY: docker
docker: docker-lint docker-build docker-smoke

.PHONY: docker-lint
docker-lint:
	@if ! command -v hadolint >/dev/null 2>&1; then $(call missing,hadolint,hadolint); else \
		echo "==> hadolint"; hadolint --failure-threshold warning Dockerfile; \
	fi

# Depends on setup because VERSION is read from the INSTALLED package. Without a
# venv it resolves to an empty string, and an empty --build-arg overrides the
# Dockerfile's default rather than falling back to it — which fails the build
# with "setuptools-scm was unable to detect version".
.PHONY: docker-build
docker-build: setup
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,docker build); else \
		set -e; echo "==> docker build"; \
		docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$(VERSION)" \
			$(DOCKER_BUILD_ARGS) -t $(IMAGE) .; \
	fi

.PHONY: docker-smoke
docker-smoke: setup
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,image smoke test); else \
		set -e; echo "==> image smoke test"; \
		docker image inspect $(IMAGE) >/dev/null \
			|| { echo "FAIL: $(IMAGE) not built -- run 'make docker-build' first"; exit 1; }; \
		docker run --rm $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    reports version $(VERSION)"; \
		docker run --rm --entrypoint python $(IMAGE) -c \
			'import ecobee_importer as m; print(m.__version__)' >/dev/null; \
		echo "    package imports inside the image"; \
		docker run --rm --entrypoint python $(IMAGE) -c \
			'from zoneinfo import ZoneInfo; ZoneInfo("America/New_York"); ZoneInfo("UTC")'; \
		echo "    IANA time zones resolve inside the image"; \
		docker run --rm --read-only $(IMAGE) --version >/dev/null; \
		echo "    starts with a READ-ONLY rootfs, as the Deployment runs it"; \
		out="$$(docker run --rm $(IMAGE) 2>&1 || true)"; \
		case "$$out" in \
			*"No credentials at"*) echo "    refuses to start unconfigured, with the expected message";; \
			*) echo "FAIL: unconfigured run said: $$out"; exit 1;; \
		esac; \
		if docker run --rm $(IMAGE) >/dev/null 2>&1; then \
			echo "FAIL: expected a non-zero exit with no credentials"; exit 1; fi; \
		echo "    runs as uid $$(docker run --rm --entrypoint python $(IMAGE) -c 'import os;print(os.getuid())')"; \
	fi

.PHONY: scan
scan:
	@if ! command -v grype >/dev/null 2>&1; then $(call missing,grype,grype); else \
		set -e; echo "==> grype"; \
		docker image inspect $(IMAGE) >/dev/null 2>&1 \
			|| { echo "FAIL: $(IMAGE) not built -- run 'make docker' first"; exit 1; }; \
		grype $(IMAGE) --only-fixed --fail-on high; \
	fi

# ----------------------------------------------------------------- gates ----

.PHONY: check
check: lint actionlint actions-pinned test helm docker
	@echo
	@echo "All available checks passed."
	@if [ -z "$(REQUIRE_ALL)" ]; then \
		echo "Note: anything reported as SKIP above was not run."; fi

.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
