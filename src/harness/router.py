"""Rule-based router — no LLM in the daemon.

The router decides what to do with an inbound Message:
  - SLASH_COMMAND     /help, /status, /sessions, /end, /new — handled in slash.py
  - RESUME_SESSION    free text within idle window → resume operator's
                      most-recent session
  - NEW_SESSION       free text outside idle window → fresh engagement +
                      session
  - REJECT            unknown chat_id (caught at ingress; included here for
                      completeness so the daemon never silently drops)

Anti-pattern explicitly avoided: LLM-based intent classification at this
layer. The plan's classifier_route enum (status_query/spec_request/etc) is
v0.2 territory and only meaningful once we have a real audit corpus to
train against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from harness.config import HarnessSettings
from harness.db import Ledger
from harness.models import Engagement, Message


SLASH_RE = re.compile(r"^/([a-z][a-z0-9_-]*)(@[a-z0-9_]+)?", re.IGNORECASE)


class Intent(str, Enum):
    SLASH_COMMAND = "slash_command"
    RESUME_SESSION = "resume_session"
    NEW_SESSION = "new_session"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    intent: Intent
    engagement: Engagement
    resume_claude_session_id: str | None  # set only when intent == RESUME_SESSION
    slash_name: str | None                # e.g. "help" without leading slash
    slash_args: str | None                # everything after the command


def parse_slash(body: str) -> tuple[str, str] | None:
    """Return (name, args) for a slash command, or None if not a slash.

    Tolerates @bot suffix on the command name (Telegram group-style).
    """
    stripped = body.strip()
    m = SLASH_RE.match(stripped)
    if not m:
        return None
    head = m.group(1).lower()
    rest = stripped[m.end():].strip()
    return head, rest


async def decide(
    message: Message,
    *,
    ledger: Ledger,
    settings: HarnessSettings,
) -> RouteDecision:
    """Resolve the message to an Intent + Engagement.

    Side effects: may create a fresh Engagement row if no active one within
    the idle window. Does NOT spawn a session — that's the caller's job.
    """
    body = message.body or ""
    slash = parse_slash(body)

    # Slash commands always run in the operator's most-recent engagement,
    # creating one if none exists. They never themselves spawn `claude`.
    active = await ledger.get_active_engagement(
        message.operator_id, idle_window_seconds=settings.idle_window_seconds
    )
    if slash is not None:
        engagement = active or await ledger.create_engagement(
            message.operator_id, summary=f"slash: /{slash[0]}"
        )
        return RouteDecision(
            intent=Intent.SLASH_COMMAND,
            engagement=engagement,
            resume_claude_session_id=None,
            slash_name=slash[0],
            slash_args=slash[1] or None,
        )

    # Non-slash text within the idle window → resume the engagement's
    # current session (if any).
    if active is not None and active.current_session_id is not None:
        sess = await ledger._get_session(active.current_session_id)  # noqa: SLF001
        return RouteDecision(
            intent=Intent.RESUME_SESSION,
            engagement=active,
            resume_claude_session_id=sess.claude_session_id,
            slash_name=None,
            slash_args=None,
        )

    # Either no active engagement (first message, or last one ended), or
    # active but no session yet → fresh session in (new or existing)
    # engagement.
    engagement = active or await ledger.create_engagement(
        message.operator_id, summary=body[:80]
    )
    return RouteDecision(
        intent=Intent.NEW_SESSION,
        engagement=engagement,
        resume_claude_session_id=None,
        slash_name=None,
        slash_args=None,
    )
