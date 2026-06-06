"""Telegram outbound — chunked, reply-threaded, engagement-prefixed.

Telegram's per-message body limit is 4096 characters. Anything longer must
be split. We do the split on character boundaries (not bytes) and on the
last newline within the window if one exists, so paragraphs survive.
"""
from __future__ import annotations

import logging

# Telegram cap is 4096 chars per send; we leave headroom for the prefix
# and any trailing "(part N/M)" indicator.
_MAX_CHUNK = 3800

log = logging.getLogger(__name__)


def format_prefix(*, engagement_id: int, session_id: int | None, mode: str) -> str:
    """Operator-visible header so they always know which session a reply is from."""
    sess = f"s{session_id}" if session_id is not None else "—"
    return f"[engagement #{engagement_id} · session {sess} · {mode}]\n"


def chunk_text(body: str, *, max_chunk: int = _MAX_CHUNK) -> list[str]:
    """Split body into telegram-safe chunks.

    Prefer to break at the last newline within the window; fall back to a
    hard split if no newline exists. Empty input → single empty chunk
    (caller decides whether to send).
    """
    if len(body) <= max_chunk:
        return [body]
    out: list[str] = []
    remaining = body
    while len(remaining) > max_chunk:
        cut = remaining.rfind("\n", 0, max_chunk)
        if cut == -1 or cut < max_chunk // 2:
            cut = max_chunk
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


def with_part_marker(chunks: list[str]) -> list[str]:
    if len(chunks) <= 1:
        return chunks
    n = len(chunks)
    return [f"{c}\n\n(part {i + 1}/{n})" for i, c in enumerate(chunks)]


async def send_reply(
    bot,
    *,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None,
    engagement_id: int,
    session_id: int | None,
    mode: str,
) -> list[int]:
    """Send `text` to the operator with engagement/session prefix, chunked.

    Returns the list of telegram message_ids sent (so caller can persist
    for /sessions threading later).
    """
    prefix = format_prefix(engagement_id=engagement_id, session_id=session_id, mode=mode)
    body = prefix + (text or "(empty reply)")
    chunks = with_part_marker(chunk_text(body))
    sent_ids: list[int] = []
    for i, chunk in enumerate(chunks):
        # Only thread reply_to onto the first chunk; subsequent chunks
        # threaded onto the previous chunk would clutter the operator's
        # phone.
        kwargs = {}
        if i == 0 and reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        msg = await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
        sent_ids.append(msg.message_id)
    return sent_ids
