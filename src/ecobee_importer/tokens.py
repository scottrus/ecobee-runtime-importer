"""Token storage.

Exactly one process may write these values. See ARCHITECTURE.md §4.2: if Auth0
rotates the refresh token and a second writer presents the old one, the account
is locked out until a human repeats the interactive login.

Nothing in this module logs a token value, at any level.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

_LOGGER = logging.getLogger(__name__)

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# Secret / JSON keys. Changing these breaks existing deployments silently, so
# they are named once, here, and referenced everywhere else.
KEY_ACCESS = "access_token"
KEY_REFRESH = "refresh_token"
KEY_API = "api_key"

# Placeholders that documentation and examples have used. A Secret created with
# one of these is accepted by kubectl without complaint and only fails much
# later, as an `invalid_grant` that reads like a revoked token rather than one
# that was never real. Naming them here turns that into an immediate, obvious
# startup error.
PLACEHOLDERS = frozenset(
    {
        "paste_here",
        "paste_token_here",
        "replace_me",
        "<value>",
        "<from bootstrap>",
        "changeme",
        "todo",
    }
)


def reject_placeholder(value: str, source: str) -> None:
    if value.strip().lower() in PLACEHOLDERS:
        raise ValueError(
            f"{source} contains the placeholder {value.strip()!r}, not a real "
            f"refresh token. Re-create it from the credentials file that "
            f"scripts/bootstrap.py wrote — see step 2 of the README."
        )


@dataclass
class Tokens:
    refresh_token: str
    access_token: str = ""
    # Only set for accounts holding a legacy developer key, which use the older
    # PIN refresh endpoint instead of the Auth0 one. Empty for everyone else.
    api_key: str = ""

    def redacted(self) -> str:
        """A safe description for logs: shape, never content."""
        return (
            f"Tokens(refresh={'set' if self.refresh_token else 'MISSING'}, "
            f"access={'set' if self.access_token else 'empty'}, "
            f"api_key={'set' if self.api_key else 'unset'})"
        )


class TokenStore(Protocol):
    def load(self) -> Tokens: ...
    def save(self, tokens: Tokens) -> None: ...


class FileTokenStore:
    """JSON file on disk. For local development and for `bootstrap.py` output."""

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Tokens:
        if not self.path.exists():
            raise FileNotFoundError(
                f"No credentials at {self.path}. Run scripts/bootstrap.py first."
            )
        data = json.loads(self.path.read_text())
        refresh = data.get(KEY_REFRESH, "")
        if not refresh:
            raise ValueError(f"{self.path} has no {KEY_REFRESH!r}")
        reject_placeholder(refresh, str(self.path))
        return Tokens(
            refresh_token=refresh,
            access_token=data.get(KEY_ACCESS, ""),
            api_key=data.get(KEY_API, ""),
        )

    def save(self, tokens: Tokens) -> None:
        payload = {
            KEY_REFRESH: tokens.refresh_token,
            KEY_ACCESS: tokens.access_token,
        }
        if tokens.api_key:
            payload[KEY_API] = tokens.api_key

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-and-rename so a crash mid-write cannot leave a truncated file
        # where the only copy of the refresh token used to be.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        _LOGGER.info("Persisted rotated tokens to %s", self.path)


class KubernetesSecretStore:
    """Read and patch a single Secret via the in-cluster API.

    Deliberately uses `requests` against the API server rather than the
    `kubernetes` client: two calls are needed (GET and PATCH), and the
    dependency is not worth carrying for that.

    RBAC must grant get + patch on this one Secret by `resourceNames`.
    """

    def __init__(self, name: str, namespace: str = ""):
        self.name = name
        # Default to the pod's own namespace so a redeploy into a different one
        # needs no config change at all.
        self.namespace = namespace or self._own_namespace()

        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise RuntimeError(
                "ECOBEE_TOKEN_STORE=kubernetes but KUBERNETES_SERVICE_HOST is unset; "
                "this store only works inside a pod"
            )
        self.url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/secrets/{name}"
        self.ca = f"{SA_DIR}/ca.crt"
        self._sa_token_path = Path(f"{SA_DIR}/token")

    @staticmethod
    def _own_namespace() -> str:
        try:
            return Path(f"{SA_DIR}/namespace").read_text().strip()
        except OSError as err:
            raise RuntimeError(
                "ECOBEE_SECRET_NAMESPACE is unset and this pod's namespace could "
                f"not be read from {SA_DIR}/namespace: {err}"
            ) from err

    def _explain_403(self, verb: str) -> str:
        """The one misconfiguration this store cannot work around.

        The Secret's name appears in two places that must agree: this store's
        config, and the Role's `resourceNames`. Renaming the Secret without
        updating the Role produces a 403 that says nothing about why.
        """
        return (
            f"Forbidden: the ServiceAccount may not {verb} Secret "
            f"{self.namespace}/{self.name}. The Role's resourceNames must list "
            f"exactly this name — if the Secret was renamed, deploy/rbac.yaml "
            f"needs the same change."
        )

    def _headers(self, content_type: str | None = None) -> dict:
        # Re-read on every call: projected ServiceAccount tokens are rotated in
        # place, and a cached one starts failing after ~1 hour.
        headers = {"Authorization": f"Bearer {self._sa_token_path.read_text().strip()}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def load(self) -> Tokens:
        resp = requests.get(self.url, headers=self._headers(), verify=self.ca, timeout=30)
        if resp.status_code == 403:
            raise PermissionError(self._explain_403("get"))
        resp.raise_for_status()
        data = resp.json().get("data", {})

        def decode(key: str) -> str:
            raw = data.get(key)
            return base64.b64decode(raw).decode().strip() if raw else ""

        refresh = decode(KEY_REFRESH)
        if not refresh:
            raise ValueError(
                f"Secret {self.namespace}/{self.name} has no {KEY_REFRESH!r} key. "
                f"Expected keys: {KEY_REFRESH}, {KEY_ACCESS} (optional), "
                f"{KEY_API} (optional)."
            )
        reject_placeholder(refresh, f"Secret {self.namespace}/{self.name}")
        return Tokens(
            refresh_token=refresh,
            access_token=decode(KEY_ACCESS),
            api_key=decode(KEY_API),
        )

    def save(self, tokens: Tokens) -> None:
        def encode(value: str) -> str:
            return base64.b64encode(value.encode()).decode()

        patch = {
            "data": {
                KEY_REFRESH: encode(tokens.refresh_token),
                KEY_ACCESS: encode(tokens.access_token),
            }
        }
        resp = requests.patch(
            self.url,
            headers=self._headers("application/strategic-merge-patch+json"),
            json=patch,
            verify=self.ca,
            timeout=30,
        )
        if resp.status_code == 403:
            # Fatal in a specific way: the token has already been rotated at
            # Auth0, so failing to persist it here means the stored value is
            # now dead and the next restart needs a fresh interactive login.
            raise PermissionError(self._explain_403("patch"))
        resp.raise_for_status()
        _LOGGER.info(
            "Persisted rotated tokens to Secret %s/%s", self.namespace, self.name
        )


def build_store(cfg) -> TokenStore:
    if cfg.token_store == "kubernetes":
        return KubernetesSecretStore(cfg.secret_name, cfg.secret_namespace)
    return FileTokenStore(cfg.token_file)
