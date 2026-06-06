"""Router + slash command tests."""
from __future__ import annotations

import pytest

from harness.config import HarnessSettings
from harness.db import Ledger
from harness.router import Intent, decide, parse_slash
from harness import slash as slash_mod


from pathlib import Path
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


@pytest.fixture
def settings():
    return HarnessSettings(_env_file=None)


def test_parse_slash_basic():
    assert parse_slash("/help") == ("help", "")
    assert parse_slash("/status") == ("status", "")
    assert parse_slash("/end please") == ("end", "please")
    assert parse_slash("/help@overwatch_bot") == ("help", "")
    assert parse_slash("not a slash") is None
    assert parse_slash("") is None


async def test_decide_first_message_is_new_session(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    msg = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram",
        channel_msg_id="1", body="hello",
    )
    decision = await decide(msg, ledger=ledger, settings=settings)
    assert decision.intent == Intent.NEW_SESSION
    assert decision.engagement is not None
    assert decision.resume_claude_session_id is None


async def test_decide_resume_within_idle_window(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    msg = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram", channel_msg_id="1", body="hello",
    )
    d1 = await decide(msg, ledger=ledger, settings=settings)
    # Spawn establishes session + claude_session_id
    sess = await ledger.create_session(d1.engagement.engagement_id, prompt_excerpt="hello")
    await ledger.attach_claude_session_id(sess.session_id, "claude-uuid-1")
    # Second message within window → resume
    msg2 = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram", channel_msg_id="2", body="and 2+2?",
    )
    d2 = await decide(msg2, ledger=ledger, settings=settings)
    assert d2.intent == Intent.RESUME_SESSION
    assert d2.engagement.engagement_id == d1.engagement.engagement_id
    assert d2.resume_claude_session_id == "claude-uuid-1"


async def test_decide_slash_creates_engagement_if_none(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    msg = await ledger.insert_message(
        operator_id=op.operator_id, channel_kind="telegram", channel_msg_id="1", body="/help",
    )
    d = await decide(msg, ledger=ledger, settings=settings)
    assert d.intent == Intent.SLASH_COMMAND
    assert d.slash_name == "help"
    assert d.slash_args is None
    assert d.engagement is not None  # auto-created


async def test_slash_dispatch_help(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    reply = await slash_mod.dispatch("help", None, ledger=ledger, settings=settings, engagement=e)
    assert "/help" in reply and "/end" in reply


async def test_slash_dispatch_unknown(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    reply = await slash_mod.dispatch("zzz", None, ledger=ledger, settings=settings, engagement=e)
    assert "unknown command" in reply


async def test_slash_end_flips_engagement(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    reply = await slash_mod.dispatch("end", None, ledger=ledger, settings=settings, engagement=e)
    assert "ended" in reply
    e_after = await ledger._get_engagement(e.engagement_id)  # noqa: SLF001
    assert not e_after.is_active


async def test_slash_sessions_lists(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    s1 = await ledger.create_session(e.engagement_id, prompt_excerpt="first")
    await ledger.attach_claude_session_id(s1.session_id, "uuid-aaaaaaaa-bbb")
    await ledger.finalize_session(s1.session_id, status="completed", exit_code=0,
                                  response_excerpt="r", duration_ms=42)
    reply = await slash_mod.dispatch("sessions", None, ledger=ledger, settings=settings, engagement=e)
    assert "s1" in reply and "completed" in reply and "uuid-aaa"[:8] in reply


async def test_slash_status_without_heartbeat(ledger, settings):
    op = await ledger.upsert_operator(channel_kind="telegram", channel_user_id="42")
    e = await ledger.create_engagement(op.operator_id)
    reply = await slash_mod.dispatch("status", None, ledger=ledger, settings=settings, engagement=e)
    assert "no heartbeat" in reply
