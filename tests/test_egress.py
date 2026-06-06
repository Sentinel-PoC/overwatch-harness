"""Egress chunking + send_reply tests (no real telegram calls)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.egress.telegram import chunk_text, format_prefix, send_reply, with_part_marker


def test_chunk_below_limit_single_chunk():
    out = chunk_text("hello", max_chunk=100)
    assert out == ["hello"]


def test_chunk_breaks_on_newline_when_available():
    body = ("paragraph one\n" * 10) + ("paragraph two\n" * 10)
    out = chunk_text(body, max_chunk=80)
    assert all(len(c) <= 80 for c in out)
    # No chunk should end mid-word: cut should land on a newline boundary
    assert all(c.endswith("\n") or "\n" in c for c in out)


def test_chunk_hard_split_when_no_newline():
    body = "x" * 250
    out = chunk_text(body, max_chunk=100)
    assert [len(c) for c in out] == [100, 100, 50]


def test_part_marker_added_only_when_multi():
    assert with_part_marker(["x"]) == ["x"]
    out = with_part_marker(["a", "b", "c"])
    assert all("(part" in c for c in out)
    assert "(part 1/3)" in out[0]
    assert "(part 3/3)" in out[2]


def test_format_prefix():
    assert format_prefix(engagement_id=7, session_id=3, mode="resume") == "[engagement #7 · session s3 · resume]\n"
    assert format_prefix(engagement_id=7, session_id=None, mode="new") == "[engagement #7 · session — · new]\n"


@pytest.mark.asyncio
async def test_send_reply_threads_first_only_and_returns_ids():
    bot = MagicMock()
    sent_msg = MagicMock(message_id=999)
    bot.send_message = AsyncMock(return_value=sent_msg)

    ids = await send_reply(
        bot, chat_id=42, text="short reply",
        reply_to_message_id=11, engagement_id=1, session_id=2, mode="new",
    )
    assert ids == [999]
    args, kwargs = bot.send_message.call_args
    assert kwargs["chat_id"] == 42
    assert "[engagement #1 · session s2 · new]" in kwargs["text"]
    assert kwargs["reply_to_message_id"] == 11


@pytest.mark.asyncio
async def test_send_reply_chunks_long_message_threads_first_only():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=i) for i in (1, 2, 3, 4)])
    long = "y" * 12000  # forces 4 chunks at default cap
    ids = await send_reply(
        bot, chat_id=42, text=long,
        reply_to_message_id=999, engagement_id=1, session_id=None, mode="new",
    )
    assert len(ids) >= 3
    # Only first call carries reply_to_message_id
    calls = bot.send_message.await_args_list
    assert calls[0].kwargs.get("reply_to_message_id") == 999
    for c in calls[1:]:
        assert "reply_to_message_id" not in c.kwargs


@pytest.mark.asyncio
async def test_send_reply_handles_empty_text():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    ids = await send_reply(bot, chat_id=1, text="", reply_to_message_id=None,
                           engagement_id=1, session_id=1, mode="new")
    assert ids == [1]
    args, kwargs = bot.send_message.call_args
    assert "(empty reply)" in kwargs["text"]
