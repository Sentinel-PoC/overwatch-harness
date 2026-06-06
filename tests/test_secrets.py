"""Secrets tests — hvac is mocked; no real Vault calls."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_client_returning(payloads: dict[str, dict]):
    client = MagicMock()
    client.is_authenticated.return_value = True

    def _read(path, **kwargs):
        full = f"secret/data/{path}"
        if full not in payloads:
            from hvac.exceptions import InvalidPath
            raise InvalidPath(f"missing {full}")
        return {"data": {"data": payloads[full]}}

    client.secrets.kv.v2.read_secret_version.side_effect = _read
    return client


def test_load_secrets_happy_path(monkeypatch):
    from harness.config import HarnessSettings
    from harness import secrets as secrets_mod

    settings = HarnessSettings(_env_file=None)
    payloads = {
        settings.vault_telegram_token_path: {settings.vault_telegram_token_field: "tg-tok"},
        settings.vault_plane_key_path: {settings.vault_plane_key_field: "plane-tok"},
        settings.vault_langfuse_path: {
            "public_key": "pk-lf-test",
            "secret_key": "sk-lf-test",
            "baseurl": "https://langfuse.example.com",
        },
    }
    fake = _mock_client_returning(payloads)
    with patch.object(secrets_mod, "hvac") as hvac_mod:
        hvac_mod.Client.return_value = fake
        s = secrets_mod.load_secrets(settings)
    assert s.telegram_bot_token == "tg-tok"
    assert s.plane_api_key == "plane-tok"
    assert s.langfuse_public_key == "pk-lf-test"
    assert s.langfuse_secret_key == "sk-lf-test"
    assert s.langfuse_host == "https://langfuse.example.com"


def test_load_secrets_missing_telegram_raises(monkeypatch):
    from harness.config import HarnessSettings
    from harness import secrets as secrets_mod

    settings = HarnessSettings(_env_file=None)
    payloads = {
        settings.vault_telegram_token_path: {"OTHER_FIELD": "x"},
        settings.vault_plane_key_path: {settings.vault_plane_key_field: "plane-tok"},
        settings.vault_langfuse_path: {"public_key": "pk"},
    }
    fake = _mock_client_returning(payloads)
    with patch.object(secrets_mod, "hvac") as hvac_mod:
        hvac_mod.Client.return_value = fake
        with pytest.raises(secrets_mod.SecretsError, match="Telegram token field"):
            secrets_mod.load_secrets(settings)


def test_load_secrets_unauthenticated_raises():
    from harness.config import HarnessSettings
    from harness import secrets as secrets_mod

    settings = HarnessSettings(_env_file=None)
    fake = MagicMock()
    fake.is_authenticated.return_value = False
    with patch.object(secrets_mod, "hvac") as hvac_mod:
        hvac_mod.Client.return_value = fake
        with pytest.raises(secrets_mod.SecretsError, match="did not authenticate"):
            secrets_mod.load_secrets(settings)


def test_load_secrets_langfuse_optional(monkeypatch):
    from harness.config import HarnessSettings
    from harness import secrets as secrets_mod

    settings = HarnessSettings(_env_file=None)
    payloads = {
        settings.vault_telegram_token_path: {settings.vault_telegram_token_field: "tg-tok"},
        settings.vault_plane_key_path: {settings.vault_plane_key_field: "plane-tok"},
        # langfuse path absent — daemon should warn but not fail
    }
    fake = _mock_client_returning(payloads)
    with patch.object(secrets_mod, "hvac") as hvac_mod:
        hvac_mod.Client.return_value = fake
        s = secrets_mod.load_secrets(settings)
    # All three langfuse fields should be None when the Vault path is absent
    assert s.langfuse_public_key is None
    assert s.langfuse_secret_key is None
    assert s.langfuse_host is None
