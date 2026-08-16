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
DEPLOY ?= deploy

# Read from the package rather than duplicated here, so the smoke test asserts the
# image really carries the version this working tree claims.
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/ecobee_importer/__init__.py)

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
	@echo "  make lint            ruff check, ruff format --check"
	@echo "  make fmt             apply ruff formatting and autofixes"
	@echo "  make test            pytest"
	@echo "  make actionlint      validate workflow syntax, expressions, run: blocks"
	@echo "  make actions-pinned  every uses: is SHA-pinned with a version comment"
	@echo "  make manifests       kustomize build, kubeconform, image-tag agreement"
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

# ------------------------------------------------------------- manifests ----

.PHONY: manifests
manifests: manifests-build manifests-schema manifests-image

# `kubectl kustomize` and the standalone `kustomize` are equivalent here; accept
# either so this works on a laptop with only one of them.
KUSTOMIZE := $(shell command -v kustomize 2>/dev/null || echo "")
KUBECTL   := $(shell command -v kubectl 2>/dev/null || echo "")

define render
if [ -n "$(KUSTOMIZE)" ]; then kustomize build $(DEPLOY); \
elif [ -n "$(KUBECTL)" ]; then kubectl kustomize $(DEPLOY); \
else exit 127; fi
endef

.PHONY: manifests-build
manifests-build:
	@if [ -z "$(KUSTOMIZE)$(KUBECTL)" ]; then $(call missing,kustomize,kustomize build); else \
		set -e; echo "==> kustomize build"; \
		$(render) > /tmp/eri-manifests.yaml; \
		for kind in Namespace ConfigMap ServiceAccount Role RoleBinding Deployment Service VMServiceScrape VMRule; do \
			grep -q "^kind: $$kind$$" /tmp/eri-manifests.yaml \
				|| { echo "FAIL: $$kind missing from the rendered output"; exit 1; }; \
		done; \
		if grep -q "^kind: Secret$$" /tmp/eri-manifests.yaml; then \
			echo "FAIL: a Secret rendered. The token Secret is created out-of-band and"; \
			echo "      rotated in place by the importer -- applying one from the repo"; \
			echo "      would overwrite a live token with a stale one."; exit 1; fi; \
		grep -q "replicas: 1" /tmp/eri-manifests.yaml \
			|| { echo "FAIL: replicas must be 1 -- a second replica is a second token writer"; exit 1; }; \
		grep -q "type: Recreate" /tmp/eri-manifests.yaml \
			|| { echo "FAIL: strategy must be Recreate, so a rollout never runs two token writers"; exit 1; }; \
		echo "    all expected kinds rendered, no Secret, single writer preserved"; \
	fi

.PHONY: manifests-schema
manifests-schema:
	@if [ -z "$(KUSTOMIZE)$(KUBECTL)" ]; then $(call missing,kustomize,kubeconform); \
	elif ! command -v kubeconform >/dev/null 2>&1; then \
		$(call missing,kubeconform,kubeconform); \
		echo "     install with: brew install kubeconform"; \
	else \
		set -e; echo "==> kubeconform"; \
		$(render) | kubeconform -strict -summary -schema-location default \
			-skip VMServiceScrape,VMRule; \
	fi

# The README tells people to clone and `kubectl apply -k deploy/` against a
# published image. That only works if the manifest names a tag the release
# workflow actually pushed, so the two must agree in the working tree.
.PHONY: manifests-image
manifests-image:
	@echo "==> deployment image tag matches the package version"
	@tag=$$(grep -oE 'image: ghcr\.io/[^:]+:[^ ]+' $(DEPLOY)/deployment.yaml | sed 's/.*://'); \
	if [ "$$tag" != "$(VERSION)" ]; then \
		echo "FAIL: deploy/deployment.yaml pins :$$tag but the package is $(VERSION)."; \
		echo "      A clone would deploy a different build than this tree."; exit 1; \
	fi; \
	echo "    both are $(VERSION)"

# ---------------------------------------------------------------- docker ----

.PHONY: docker
docker: docker-lint docker-build docker-smoke

.PHONY: docker-lint
docker-lint:
	@if ! command -v hadolint >/dev/null 2>&1; then $(call missing,hadolint,hadolint); else \
		echo "==> hadolint"; hadolint --failure-threshold warning Dockerfile; \
	fi

.PHONY: docker-build
docker-build:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,docker build); else \
		set -e; echo "==> docker build"; docker build $(DOCKER_BUILD_ARGS) -t $(IMAGE) .; \
	fi

.PHONY: docker-smoke
docker-smoke:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,image smoke test); else \
		set -e; echo "==> image smoke test"; \
		docker image inspect $(IMAGE) >/dev/null \
			|| { echo "FAIL: $(IMAGE) not built -- run 'make docker-build' first"; exit 1; }; \
		docker run --rm $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    reports version $(VERSION)"; \
		docker run --rm --entrypoint python $(IMAGE) -c \
			'import ecobee_importer as m; print(m.__version__)' >/dev/null; \
		echo "    package imports inside the image"; \
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
check: lint actionlint actions-pinned test manifests docker
	@echo
	@echo "All available checks passed."
	@if [ -z "$(REQUIRE_ALL)" ]; then \
		echo "Note: anything reported as SKIP above was not run."; fi

.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
