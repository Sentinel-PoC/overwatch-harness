"""Plane API client — engagement issue + per-session comment.

Audit principle: every spawn lands in the sqlite ledger BEFORE invoking
claude. Plane is the operator-visible audit surface for the engagement
itself; sqlite is the source of truth.

Failure mode: if Plane is down, the daemon does NOT block — it logs the
failure and continues. The sqlite ledger is the audit-trail-of-record;
Plane sync is best-effort (operator can backfill from sqlite later).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

log = logging.getLogger(__name__)


class PlaneClient:
    """Thin httpx wrapper for Plane workspaces/projects/issues + comments.

    Uses x-api-key auth per CLAUDE.md. TLS verify off when Vault settings
    say so (operator's self-signed cert posture).
    """

    def __init__(
        self,
        *,
        base_url: str,
        workspace: str,
        project_id: str,
        api_key: str,
        verify_tls: bool = False,
    ):
        self._base = base_url.rstrip("/")
        self._ws = workspace
        self._project = project_id
        self._key = api_key
        self._verify = verify_tls

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            verify=self._verify,
            timeout=10.0,
            headers={"x-api-key": self._key, "Content-Type": "application/json"},
        ) as client:
            yield client

    async def create_engagement_issue(
        self,
        *,
        title: str,
        description_html: str,
        priority: str = "medium",
    ) -> tuple[str | None, int | None]:
        """Returns (issue_uuid, sequence_id) — both None on failure."""
        url = f"{self._base}/api/v1/workspaces/{self._ws}/projects/{self._project}/issues/"
        try:
            async with self._client() as c:
                resp = await c.post(url, json={
                    "name": title,
                    "description_html": description_html,
                    "priority": priority,
                })
                resp.raise_for_status()
                data = resp.json()
            return data.get("id"), data.get("sequence_id")
        except Exception as exc:
            log.warning("plane: create_engagement_issue failed: %s", exc)
            return None, None

    async def comment(self, issue_id: str, html: str) -> bool:
        url = f"{self._base}/api/v1/workspaces/{self._ws}/projects/{self._project}/issues/{issue_id}/comments/"
        try:
            async with self._client() as c:
                resp = await c.post(url, json={"comment_html": html})
                resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("plane: comment(issue=%s) failed: %s", issue_id, exc)
            return False
