"""Dataclasses mirroring the sqlite ledger rows.

Read-side only — writes go through ledger.py with explicit sql so we don't
build an ORM accidentally. The point of these classes is type-safe access
to query results, not magic persistence.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Operator:
    operator_id: str            # "op:tg:12345"
    channel_kind: str           # 'telegram'|'discord'|'direct'
    channel_user_id: str
    display_name: str | None
    plane_actor_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Engagement:
    engagement_id: int
    operator_id: str
    started_at: str
    last_activity_at: str
    ended_at: str | None
    mode: str                   # 'oneshot'|'continuous'|'paused'|'ended'
    current_session_id: int | None
    plane_issue_id: str | None
    plane_issue_seq: int | None
    summary: str | None

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True, slots=True)
class Session:
    session_id: int
    engagement_id: int
    claude_session_id: str | None     # set after first stream chunk
    parent_session_id: int | None
    started_at: str
    ended_at: str | None
    status: str                       # spawning|running|completed|failed|interrupted|quota_exhausted
    prompt_excerpt: str
    response_excerpt: str | None
    exit_code: int | None
    langfuse_trace_id: str | None
    max_quota_reset_window: str | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class Message:
    message_id: int
    operator_id: str
    engagement_id: int | None
    session_id: int | None
    channel_kind: str
    channel_msg_id: str
    received_at: str
    body: str
    reply_to_msg_id: str | None
    classifier_route: str | None
    classifier_version: str | None
    prompt_hash: str | None
    reply_text: str | None
    status: str                       # received|classifying|routed|failed|dead_letter|done
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class Heartbeat:
    heartbeat_id: int
    ts: str
    pid: int
    state: str                        # starting|degraded|healthy|quota_throttled|shutting_down
    in_flight_count: int
    notes: str | None
