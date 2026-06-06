"""Vault-backed secret reader.

Standing principle: secrets come from Vault, never from env. config.py holds
the *paths*; this module resolves those paths to actual values at startup
and keeps them in a frozen dataclass so they don't leak into pydantic
model dumps.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import hvac
from hvac.exceptions import InvalidPath as _VaultInvalidPath

from harness.config import HarnessSettings

log = logging.getLogger(__name__)


class SecretsError(RuntimeError):
    """Raised when a required Vault path is missing or unreadable."""


@dataclass(frozen=True, slots=True)
class HarnessSecrets:
    telegram_bot_token: str
    plane_api_key: str
    # Langfuse telemetry keys — resolved from Vault at startup so the spawner
    # can inject them into every `claude -p` subprocess env.  All three are
    # None when the Vault path is absent (daemon continues in degraded-trace
    # mode; existing operator-env fallback still works if set).
    langfuse_public_key: Optional[str]
    langfuse_secret_key: Optional[str]
    langfuse_host: Optional[str]


def _resolve_vault_token() -> str | None:
    """Token preference: VAULT_TOKEN env > ~/.vault-token file.

    The systemd-user unit deliberately does NOT bake VAULT_TOKEN into its
    EnvironmentFile (rotation pain + 0600-on-disk surface). Instead we
    read ~/.vault-token, which the operator's claude-vault-renew timer
    keeps fresh.
    """
    tok = os.environ.get("VAULT_TOKEN")
    if tok:
        return tok.strip() or None
    token_file = Path.home() / ".vault-token"
    if token_file.exists():
        return token_file.read_text().strip() or None
    return None


def _client(settings: HarnessSettings) -> hvac.Client:
    client = hvac.Client(
        url=settings.vault_addr,
        token=_resolve_vault_token(),
        verify=not settings.vault_skip_verify,
    )
    try:
        authed = client.is_authenticated()
    except Exception as exc:
        raise SecretsError(f"Vault at {settings.vault_addr} unreachable: {exc}") from exc
    if not authed:
        raise SecretsError(
            f"Vault at {settings.vault_addr} did not authenticate the daemon's "
            f"VAULT_TOKEN. Check ~/.vault-token or the systemd unit's environment."
        )
    return client


def _read_kv2(client: hvac.Client, path: str) -> dict[str, Any]:
    """Read a KV v2 secret. `path` is the full API path (secret/data/...)."""
    if not path.startswith("secret/data/"):
        raise SecretsError(
            f"vault path {path!r} does not start with 'secret/data/' — "
            f"only KV v2 mount 'secret/' is supported in v0.1."
        )
    relative = path[len("secret/data/"):]
    try:
        resp = client.secrets.kv.v2.read_secret_version(path=relative, raise_on_deleted_version=True)
    except _VaultInvalidPath as exc:
        raise SecretsError(f"vault path {path!r} not found") from exc
    return resp["data"]["data"]


def load_secrets(settings: HarnessSettings) -> HarnessSecrets:
    """Resolve all Vault-backed values declared in HarnessSettings.

    Fails fast — daemon must enter degraded state rather than start partial.
    """
    client = _client(settings)

    tg_data = _read_kv2(client, settings.vault_telegram_token_path)
    tg_token = tg_data.get(settings.vault_telegram_token_field)
    if not tg_token:
        raise SecretsError(
            f"Telegram token field {settings.vault_telegram_token_field!r} "
            f"not present at {settings.vault_telegram_token_path!r}"
        )

    plane_data = _read_kv2(client, settings.vault_plane_key_path)
    plane_key = plane_data.get(settings.vault_plane_key_field)
    if not plane_key:
        raise SecretsError(
            f"Plane API key field {settings.vault_plane_key_field!r} "
            f"not present at {settings.vault_plane_key_path!r}"
        )

    lf_public_key: Optional[str] = None
    lf_secret_key: Optional[str] = None
    lf_host: Optional[str] = None
    try:
        lf_data = _read_kv2(client, settings.vault_langfuse_path)
        lf_public_key = lf_data.get("public_key") or None
        lf_secret_key = lf_data.get("secret_key") or None
        lf_host = lf_data.get("baseurl") or None
        if lf_public_key and lf_secret_key and lf_host:
            log.info("vault: resolved langfuse keys from %s", settings.vault_langfuse_path)
        else:
            log.warning(
                "vault path %s present but missing expected fields "
                "(public_key, secret_key, baseurl); langfuse traces may be incomplete",
                settings.vault_langfuse_path,
            )
    except SecretsError:
        log.warning(
            "vault path %s not present; langfuse traces not injected into spawned processes",
            settings.vault_langfuse_path,
        )

    log.info(
        "vault: resolved 2 required secrets, langfuse keys present=%s",
        all([lf_public_key, lf_secret_key, lf_host]),
    )
    return HarnessSecrets(
        telegram_bot_token=tg_token,
        plane_api_key=plane_key,
        langfuse_public_key=lf_public_key,
        langfuse_secret_key=lf_secret_key,
        langfuse_host=lf_host,
    )
