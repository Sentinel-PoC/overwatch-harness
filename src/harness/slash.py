"""Slash command handlers — pure functions over Ledger state.

Each handler returns a single string reply (chunked by egress.telegram if
long). They never spawn `claude` and never block on network.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from harness.config import HarnessSettings
from harness.db import Ledger
from harness.models import Engagement

log = logging.getLogger(__name__)


HelpFn = Callable[[Ledger, HarnessSettings, Engagement, str | None], Awaitable[str]]


async def cmd_help(ledger: Ledger, settings: HarnessSettings, engagement: Engagement, args: str | None) -> str:
    return (
        "overwatch-harness — commands\n"
        "  /help                this message\n"
        "  /status              daemon health + in-flight session count\n"
        "  /sessions            recent sessions in this engagement\n"
        "  /end                 close the active engagement\n"
        "  /new                 force-start a new engagement on next message\n\n"
        "Anything else opens or resumes a Claude Code session. "
        f"Idle window: {settings.idle_window_seconds // 60}m."
    )


async def cmd_status(ledger: Ledger, settings: HarnessSettings, engagement: Engagement, args: str | None) -> str:
    hb = await ledger.latest_heartbeat()
    if hb is None:
        return "status: no heartbeat yet (daemon just started)"
    return (
        f"status: {hb.state}  in-flight={hb.in_flight_count}  "
        f"pid={hb.pid}  ts={hb.ts}\n"
        f"engagement #{engagement.engagement_id} "
        f"({'active' if engagement.is_active else 'ended'})  "
        f"current_session={engagement.current_session_id or 'none'}"
    )


async def cmd_sessions(ledger: Ledger, settings: HarnessSettings, engagement: Engagement, args: str | None) -> str:
    async with ledger._conn.execute(  # noqa: SLF001
        "SELECT session_id, claude_session_id, status, started_at, ended_at, "
        "exit_code, duration_ms FROM session WHERE engagement_id = ? "
        "ORDER BY session_id DESC LIMIT 10",
        (engagement.engagement_id,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return f"engagement #{engagement.engagement_id}: no sessions yet"
    lines = [f"engagement #{engagement.engagement_id} — last {len(rows)} sessions:"]
    for r in rows:
        sid_short = (r["claude_session_id"] or "—")[:8]
        ended = r["ended_at"] or "running"
        rc = r["exit_code"] if r["exit_code"] is not None else "—"
        dur = f"{r['duration_ms']}ms" if r["duration_ms"] else "—"
        lines.append(
            f"  s{r['session_id']:<4} {r['status']:<10} "
            f"{sid_short}  rc={rc}  {dur}  ended={ended}"
        )
    return "\n".join(lines)


async def cmd_end(ledger: Ledger, settings: HarnessSettings, engagement: Engagement, args: str | None) -> str:
    if not engagement.is_active:
        return f"engagement #{engagement.engagement_id} is already ended"
    await ledger.end_engagement(engagement.engagement_id)
    return f"engagement #{engagement.engagement_id} ended. Next message starts a fresh engagement."


async def cmd_new(ledger: Ledger, settings: HarnessSettings, engagement: Engagement, args: str | None) -> str:
    """Force-end the active engagement so the next message creates a new one.

    Symmetry with /end — the operator-facing semantics are 'fresh slate'.
    """
    if engagement.is_active:
        await ledger.end_engagement(engagement.engagement_id)
    return "ok — next message starts a new engagement"


COMMANDS: dict[str, HelpFn] = {
    "help": cmd_help,
    "status": cmd_status,
    "sessions": cmd_sessions,
    "end": cmd_end,
    "new": cmd_new,
}


async def dispatch(
    name: str,
    args: str | None,
    *,
    ledger: Ledger,
    settings: HarnessSettings,
    engagement: Engagement,
) -> str:
    handler = COMMANDS.get(name)
    if handler is None:
        return f"unknown command: /{name}.  Try /help."
    try:
        return await handler(ledger, settings, engagement, args)
    except Exception:
        log.exception("slash /%s failed", name)
        return f"/{name} failed; check daemon logs"
