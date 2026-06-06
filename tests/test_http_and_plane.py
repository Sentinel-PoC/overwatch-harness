"""healthz + plane client tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.audit.plane import PlaneClient
from harness.db import Ledger
from harness.http import build_app
from harness.selftest import DaemonState, StateMachine


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "migrations"


@pytest.fixture
async def ledger(tmp_path):
    db = tmp_path / "h.db"
    inst = await Ledger.open(db, migrations_dir=MIGRATIONS)
    try:
        yield inst
    finally:
        await inst.close()


async def test_healthz_returns_503_in_starting_state(ledger, aiohttp_client):
    sm = StateMachine()  # state = STARTING
    app = build_app(state=sm, ledger=ledger)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    assert resp.status == 503
    body = await resp.json()
    assert body["state"] == "starting"


async def test_healthz_returns_200_when_healthy(ledger, aiohttp_client):
    sm = StateMachine()
    await sm.transition(DaemonState.HEALTHY, reason="selftest passed")
    app = build_app(state=sm, ledger=ledger)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["state"] == "healthy"


async def test_healthz_includes_heartbeat_when_present(ledger, aiohttp_client):
    sm = StateMachine()
    await sm.transition(DaemonState.HEALTHY, reason="x")
    await ledger.write_heartbeat(pid=1234, state="healthy", in_flight_count=2)
    app = build_app(state=sm, ledger=ledger)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    body = await resp.json()
    assert body["last_heartbeat"]["pid"] == 1234
    assert body["last_heartbeat"]["in_flight"] == 2


async def test_healthz_returns_503_in_degraded(ledger, aiohttp_client):
    sm = StateMachine()
    await sm.transition(DaemonState.DEGRADED, reason="claude binary missing")
    app = build_app(state=sm, ledger=ledger)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    assert resp.status == 503
    body = await resp.json()
    assert body["state"] == "degraded"
    assert body["reason"] == "claude binary missing"


@pytest.mark.asyncio
async def test_plane_client_create_issue_handles_failure_gracefully():
    pc = PlaneClient(base_url="https://plane.invalid",
                     workspace="ws", project_id="p", api_key="k")
    # No mock — let the real DNS lookup fail. The client should swallow.
    issue_id, seq = await pc.create_engagement_issue(
        title="t", description_html="<p>d</p>"
    )
    assert issue_id is None
    assert seq is None


@pytest.mark.asyncio
async def test_plane_client_comment_handles_failure_gracefully():
    pc = PlaneClient(base_url="https://plane.invalid",
                     workspace="ws", project_id="p", api_key="k")
    ok = await pc.comment("issue-uuid", "<p>x</p>")
    assert ok is False
