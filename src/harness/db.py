"""Async sqlite ledger.

Single-writer pattern: all mutations go through Ledger which owns one
aiosqlite connection. Readers may open their own short-lived connections.
This avoids the `database is locked` pathology in WAL mode under
contended writes.

The schema applied is migrations/0001_init.sql verbatim; we don't
re-declare DDL here. Migrations beyond v1 land as additional .sql files
discovered by name.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import aiosqlite

from harness.models import Engagement, Heartbeat, Message, Operator, Session

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")


def _discover_migrations(root: Path = _MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    if not root.exists():
        return out
    for p in sorted(root.glob("*.sql")):
        m = _MIGRATION_FILENAME_RE.match(p.name)
        if not m:
            continue
        out.append((int(m.group(1)), p))
    return sorted(out, key=lambda x: x[0])


async def apply_migrations(conn: aiosqlite.Connection, migrations_dir: Path = _MIGRATIONS_DIR) -> list[int]:
    """Apply any migrations newer than schema_version.MAX(version).

    Returns versions applied (empty list if up-to-date).
    """
    applied: list[int] = []
    # Bootstrap: apply 0001 unconditionally on a fresh DB; the script is IF NOT
    # EXISTS so it's a no-op on re-apply.
    discovered = _discover_migrations(migrations_dir)
    if not discovered:
        log.warning("no migrations found under %s", migrations_dir)
        return applied

    # Read current head — table may not exist yet on fresh DB
    try:
        async with conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
            head = row[0] if row and row[0] is not None else 0
    except aiosqlite.OperationalError:
        head = 0

    for version, path in discovered:
        if version <= head:
            continue
        log.info("applying migration %s", path.name)
        await conn.executescript(path.read_text())
        applied.append(version)

    await conn.commit()
    return applied


class Ledger:
    """Single-writer wrapper over aiosqlite.

    Use as `async with Ledger.open(path) as ledger:` from the daemon's main
    coroutine. Concurrent writers are funneled through an asyncio.Lock so
    we don't fight WAL contention.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, db_path: Path, *, migrations_dir: Path = _MIGRATIONS_DIR) -> "Ledger":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(conn, migrations_dir)
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> "Ledger":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- operator -----------------------------------------------------------

    async def upsert_operator(
        self,
        *,
        channel_kind: str,
        channel_user_id: str,
        display_name: str | None = None,
    ) -> Operator:
        operator_id = f"op:{channel_kind[:2]}:{channel_user_id}"
        async with self._write_lock:
            await self._conn.execute(
                """
                INSERT INTO operator (operator_id, channel_kind, channel_user_id, display_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(operator_id) DO UPDATE SET display_name=excluded.display_name
                  WHERE excluded.display_name IS NOT NULL
                """,
                (operator_id, channel_kind, channel_user_id, display_name),
            )
            await self._conn.commit()
        return await self._get_operator(operator_id)

    async def _get_operator(self, operator_id: str) -> Operator:
        async with self._conn.execute(
            "SELECT * FROM operator WHERE operator_id = ?", (operator_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"operator {operator_id} disappeared after upsert")
        return Operator(
            operator_id=row["operator_id"],
            channel_kind=row["channel_kind"],
            channel_user_id=row["channel_user_id"],
            display_name=row["display_name"],
            plane_actor_id=row["plane_actor_id"],
            created_at=row["created_at"],
        )

    # -- engagement ---------------------------------------------------------

    async def get_active_engagement(
        self,
        operator_id: str,
        *,
        idle_window_seconds: int,
    ) -> Engagement | None:
        """Return active engagement if last_activity is within the idle window.

        Outside the window → None (caller creates fresh engagement).
        """
        async with self._conn.execute(
            """
            SELECT * FROM engagement
            WHERE operator_id = ?
              AND ended_at IS NULL
              AND (julianday('now') - julianday(last_activity_at)) * 86400 <= ?
            ORDER BY last_activity_at DESC
            LIMIT 1
            """,
            (operator_id, idle_window_seconds),
        ) as cur:
            row = await cur.fetchone()
        return _engagement_from_row(row) if row else None

    async def create_engagement(self, operator_id: str, *, summary: str | None = None) -> Engagement:
        async with self._write_lock:
            cur = await self._conn.execute(
                "INSERT INTO engagement (operator_id, summary) VALUES (?, ?)",
                (operator_id, summary),
            )
            await self._conn.commit()
            engagement_id = cur.lastrowid
        return await self._get_engagement(engagement_id)

    async def _get_engagement(self, engagement_id: int) -> Engagement:
        async with self._conn.execute(
            "SELECT * FROM engagement WHERE engagement_id = ?", (engagement_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"engagement {engagement_id} not found")
        return _engagement_from_row(row)

    async def touch_engagement(self, engagement_id: int) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE engagement SET last_activity_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE engagement_id = ?",
                (engagement_id,),
            )
            await self._conn.commit()

    async def end_engagement(self, engagement_id: int) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE engagement SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "mode = 'ended' WHERE engagement_id = ?",
                (engagement_id,),
            )
            await self._conn.commit()

    # -- session ------------------------------------------------------------

    async def create_session(
        self,
        engagement_id: int,
        *,
        prompt_excerpt: str,
        parent_session_id: int | None = None,
    ) -> Session:
        async with self._write_lock:
            cur = await self._conn.execute(
                """
                INSERT INTO session (engagement_id, prompt_excerpt, parent_session_id)
                VALUES (?, ?, ?)
                """,
                (engagement_id, prompt_excerpt[:500], parent_session_id),
            )
            session_id = cur.lastrowid
            await self._conn.execute(
                "UPDATE engagement SET current_session_id = ? WHERE engagement_id = ?",
                (session_id, engagement_id),
            )
            await self._conn.commit()
        return await self._get_session(session_id)

    async def _get_session(self, session_id: int) -> Session:
        async with self._conn.execute(
            "SELECT * FROM session WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"session {session_id} not found")
        return _session_from_row(row)

    async def attach_claude_session_id(self, session_id: int, claude_session_id: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE session SET claude_session_id = ?, status = 'running' "
                "WHERE session_id = ?",
                (claude_session_id, session_id),
            )
            await self._conn.commit()

    async def finalize_session(
        self,
        session_id: int,
        *,
        status: str,
        exit_code: int | None,
        response_excerpt: str | None,
        duration_ms: int | None,
        max_quota_reset_window: str | None = None,
    ) -> None:
        async with self._write_lock:
            await self._conn.execute(
                """
                UPDATE session
                SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    status = ?, exit_code = ?, response_excerpt = ?,
                    duration_ms = ?, max_quota_reset_window = ?
                WHERE session_id = ?
                """,
                (
                    status,
                    exit_code,
                    (response_excerpt or "")[:500] or None,
                    duration_ms,
                    max_quota_reset_window,
                    session_id,
                ),
            )
            await self._conn.commit()

    # -- message ------------------------------------------------------------

    async def insert_message(
        self,
        *,
        operator_id: str,
        channel_kind: str,
        channel_msg_id: str,
        body: str,
        reply_to_msg_id: str | None = None,
        engagement_id: int | None = None,
    ) -> Message | None:
        """Insert idempotently. Returns None if (channel_kind, channel_msg_id)
        already exists — caller should treat as a replay and skip processing.
        """
        async with self._write_lock:
            try:
                cur = await self._conn.execute(
                    """
                    INSERT INTO message
                        (operator_id, engagement_id, channel_kind, channel_msg_id, body, reply_to_msg_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operator_id, engagement_id, channel_kind, channel_msg_id, body, reply_to_msg_id),
                )
                await self._conn.commit()
                message_id = cur.lastrowid
            except aiosqlite.IntegrityError:
                log.info("message %s/%s replay; skipping", channel_kind, channel_msg_id)
                return None
        return await self._get_message(message_id)

    async def _get_message(self, message_id: int) -> Message:
        async with self._conn.execute(
            "SELECT * FROM message WHERE message_id = ?", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"message {message_id} not found")
        return _message_from_row(row)

    async def update_message_status(
        self,
        message_id: int,
        *,
        status: str,
        engagement_id: int | None = None,
        session_id: int | None = None,
        classifier_route: str | None = None,
        reply_text: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        async with self._write_lock:
            await self._conn.execute(
                """
                UPDATE message
                SET status = ?,
                    engagement_id = COALESCE(?, engagement_id),
                    session_id = COALESCE(?, session_id),
                    classifier_route = COALESCE(?, classifier_route),
                    reply_text = COALESCE(?, reply_text),
                    error_summary = COALESCE(?, error_summary)
                WHERE message_id = ?
                """,
                (status, engagement_id, session_id, classifier_route, reply_text,
                 error_summary, message_id),
            )
            await self._conn.commit()

    async def list_pending_on_startup(self) -> list[Message]:
        """Replay queue: messages received before the daemon restarted that
        never made it past classification. Daemon resumes processing.
        """
        async with self._conn.execute(
            "SELECT * FROM message WHERE status IN ('received','classifying') "
            "ORDER BY received_at"
        ) as cur:
            rows = await cur.fetchall()
        return [_message_from_row(r) for r in rows]

    # -- heartbeat ---------------------------------------------------------

    async def write_heartbeat(
        self, *, pid: int, state: str, in_flight_count: int, notes: str | None = None
    ) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO heartbeat (pid, state, in_flight_count, notes) "
                "VALUES (?, ?, ?, ?)",
                (pid, state, in_flight_count, notes),
            )
            await self._conn.commit()

    async def latest_heartbeat(self) -> Heartbeat | None:
        async with self._conn.execute(
            "SELECT * FROM heartbeat ORDER BY heartbeat_id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Heartbeat(
            heartbeat_id=row["heartbeat_id"],
            ts=row["ts"],
            pid=row["pid"],
            state=row["state"],
            in_flight_count=row["in_flight_count"],
            notes=row["notes"],
        )

    async def prune_heartbeats(self, *, keep_rows: int = 1440) -> int:
        async with self._write_lock:
            cur = await self._conn.execute(
                "DELETE FROM heartbeat WHERE heartbeat_id NOT IN "
                "(SELECT heartbeat_id FROM heartbeat ORDER BY ts DESC LIMIT ?)",
                (keep_rows,),
            )
            await self._conn.commit()
        return cur.rowcount or 0


# -- row → dataclass helpers --------------------------------------------------


def _engagement_from_row(row: aiosqlite.Row) -> Engagement:
    return Engagement(
        engagement_id=row["engagement_id"],
        operator_id=row["operator_id"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        ended_at=row["ended_at"],
        mode=row["mode"],
        current_session_id=row["current_session_id"],
        plane_issue_id=row["plane_issue_id"],
        plane_issue_seq=row["plane_issue_seq"],
        summary=row["summary"],
    )


def _session_from_row(row: aiosqlite.Row) -> Session:
    return Session(
        session_id=row["session_id"],
        engagement_id=row["engagement_id"],
        claude_session_id=row["claude_session_id"],
        parent_session_id=row["parent_session_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        status=row["status"],
        prompt_excerpt=row["prompt_excerpt"],
        response_excerpt=row["response_excerpt"],
        exit_code=row["exit_code"],
        langfuse_trace_id=row["langfuse_trace_id"],
        max_quota_reset_window=row["max_quota_reset_window"],
        duration_ms=row["duration_ms"],
    )


def _message_from_row(row: aiosqlite.Row) -> Message:
    return Message(
        message_id=row["message_id"],
        operator_id=row["operator_id"],
        engagement_id=row["engagement_id"],
        session_id=row["session_id"],
        channel_kind=row["channel_kind"],
        channel_msg_id=row["channel_msg_id"],
        received_at=row["received_at"],
        body=row["body"],
        reply_to_msg_id=row["reply_to_msg_id"],
        classifier_route=row["classifier_route"],
        classifier_version=row["classifier_version"],
        prompt_hash=row["prompt_hash"],
        reply_text=row["reply_text"],
        status=row["status"],
        error_summary=row["error_summary"],
    )
