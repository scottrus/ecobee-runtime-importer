"""Tests for the token lifecycle.

This is the part of the system whose failure cannot self-heal: a lost or stale
refresh token means an interactive login with an authenticator app. See
ARCHITECTURE.md §4.
"""

import json

import pytest

from ecobee_importer.ecobee import EcobeeClient
from ecobee_importer.tokens import FileTokenStore, Tokens


class FakeStore:
    def __init__(self, tokens: Tokens):
        self.tokens = tokens
        self.saved: list[Tokens] = []

    def load(self) -> Tokens:
        return self.tokens

    def save(self, tokens: Tokens) -> None:
        self.saved.append(tokens)


def client_with(refresh="r1", access="a1", api_key=""):
    store = FakeStore(Tokens(refresh_token=refresh, access_token=access, api_key=api_key))
    return EcobeeClient(store), store


# --- FileTokenStore ------------------------------------------------------


def test_file_store_round_trip(tmp_path):
    path = tmp_path / "nested" / "credentials.json"
    store = FileTokenStore(str(path))
    store.save(Tokens(refresh_token="r1", access_token="a1"))

    loaded = store.load()
    assert loaded.refresh_token == "r1"
    assert loaded.access_token == "a1"
    # Written 0600: this file is the only copy of a credential.
    assert path.stat().st_mode & 0o777 == 0o600


def test_file_store_rejects_document_without_refresh_token(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"access_token": "a1"}))
    with pytest.raises(ValueError, match="refresh_token"):
        FileTokenStore(str(path)).load()


def test_file_store_omits_empty_api_key(tmp_path):
    path = tmp_path / "credentials.json"
    FileTokenStore(str(path)).save(Tokens(refresh_token="r1"))
    # An empty api_key must be absent, not "": its presence switches pyecobee
    # to the legacy PIN refresh endpoint.
    assert "api_key" not in json.loads(path.read_text())


def test_redacted_never_contains_the_token():
    described = Tokens(
        refresh_token="super-secret", access_token="also-secret"
    ).redacted()
    assert "super-secret" not in described
    assert "also-secret" not in described


# --- rotation handling ---------------------------------------------------


def test_rotated_refresh_token_is_persisted():
    client, store = client_with()
    client._api.refresh_token = "r2"
    client._api.access_token = "a2"

    client._persist_if_changed()

    assert len(store.saved) == 1
    assert store.saved[0].refresh_token == "r2"


def test_unchanged_tokens_are_not_rewritten():
    """Patching the Secret every cycle would be pointless API churn."""
    client, store = client_with()
    client._persist_if_changed()
    assert store.saved == []


def test_new_access_token_alone_is_persisted():
    """Auth0 need not rotate the refresh token; the access token still moves."""
    client, store = client_with()
    client._api.access_token = "a2"

    client._persist_if_changed()

    assert len(store.saved) == 1
    assert store.saved[0].refresh_token == "r1"
    assert store.saved[0].access_token == "a2"


def test_empty_refresh_token_is_refused():
    """Persisting an empty value would destroy the only copy of the credential."""
    client, store = client_with()
    client._api.refresh_token = ""
    client._api.access_token = "a2"

    client._persist_if_changed()

    assert store.saved == []


# --- refresh path selection ----------------------------------------------


def test_no_api_key_means_the_auth0_refresh_path():
    """An empty api_key must not reach pyecobee.

    pyecobee branches on `if self.api_key:` and, when set, refreshes against the
    legacy PIN endpoint — which rejects Auth0 web-flow tokens.
    """
    client, _ = client_with(api_key="")
    assert client._api.api_key is None


def test_legacy_api_key_is_passed_through():
    client, _ = client_with(api_key="legacy-key")
    assert client._api.api_key == "legacy-key"
