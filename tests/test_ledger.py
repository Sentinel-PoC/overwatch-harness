"""Async ledger tests against an ephemeral sqlite db."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.db import Ledger


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


async def test_open_applies_schema_idempotently(tmp_path):
    db = tmp_path / "h.db"
    l1 = await Ledger.open(db, migrations_dir=MIGRATIONS)
    await l1.close()
    # second open is a no-op (no errors); proves IF NOT EXISTS path
    l2 = await Ledger.open(db, migrations_dir=MIGRATIONS)
    await l2.close()


async def test_upsert_operator_creates_then_updates(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42", display_name="jim")
    assert op.operator_id == "op:te:42"
    assert op.display_name == "jim"
    op2 = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42", display_name="jim2")
    assert op2.operator_id == "op:te:42"
    assert op2.display_name == "jim2"


async def test_engagement_lifecycle(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    # No engagement yet
    e0 = await ledger.get_active_engagement(op.operator_id, idle_window_seconds=1800)
    assert e0 is None
    e1 = await ledger.create_engagement(op.operator_id, summary="first")
    assert e1.is_active
    # Within window — picks it up
    e2 = await ledger.get_active_engagement(op.operator_id, idle_window_seconds=1800)
    assert e2 is not None and e2.engagement_id == e1.engagement_id
    # End it
    await ledger.end_engagement(e1.engagement_id)
    e4 = await ledger.get_active_engagement(op.operator_id, idle_window_seconds=1800)
    assert e4 is None


async def test_session_create_and_attach_claude_id(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    s = await ledger.create_session(e.engagement_id, prompt_excerpt="hello world")
    assert s.status == "spawning"
    assert s.claude_session_id is None
    await ledger.attach_claude_session_id(s.session_id, "uuid-abc-123")
    # Re-fetch to confirm running
    e_now = await ledger.get_active_engagement(op.operator_id, idle_window_seconds=1800)
    assert e_now.current_session_id == s.session_id


async def test_session_finalize_records_exit(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    s = await ledger.create_session(e.engagement_id, prompt_excerpt="x")
    await ledger.finalize_session(
        s.session_id,
        status="completed", exit_code=0, response_excerpt="ok",
        duration_ms=150,
    )
    # Verify via raw query
    async with ledger._conn.execute("SELECT status, exit_code, duration_ms FROM session WHERE session_id=?", (s.session_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "completed"
    assert row["exit_code"] == 0
    assert row["duration_ms"] == 150


async def test_message_idempotency_on_replay(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    m1 = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram",
        channel_msg_id="upd-1", body="hi",
    )
    assert m1 is not None
    # Same channel_msg_id → idempotent skip
    m2 = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram",
        channel_msg_id="upd-1", body="hi (replay)",
    )
    assert m2 is None
    # Different msg_id → new row
    m3 = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram",
        channel_msg_id="upd-2", body="hi2",
    )
    assert m3 is not None and m3.message_id != m1.message_id


async def test_pending_replay_queue(ledger):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    m1 = await ledger.insert_message(operator_id=op.operator_id, channel_kind="telegram", channel_msg_id="1", body="x")
    m2 = await ledger.insert_message(operator_id=op.operator_id, channel_kind="telegram", channel_msg_id="2", body="y")
    # mark m1 done; m2 still 'received'
    await ledger.update_message_status(m1.message_id, status="done")
    pending = await ledger.list_pending_on_startup()
    pending_ids = {m.message_id for m in pending}
    assert pending_ids == {m2.message_id}


async def test_heartbeat_write_and_prune(ledger):
    for i in range(10):
        await ledger.write_heartbeat(pid=os.getpid(), state="healthy", in_flight_count=i)
    hb = await ledger.latest_heartbeat()
    assert hb is not None and hb.in_flight_count == 9
    deleted = await ledger.prune_heartbeats(keep_rows=3)
    assert deleted == 7
    async with ledger._conn.execute("SELECT COUNT(*) AS c FROM heartbeat") as cur:
        row = await cur.fetchone()
    assert row["c"] == 3
