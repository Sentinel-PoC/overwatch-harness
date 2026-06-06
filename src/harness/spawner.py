"""`claude -p` subprocess spawner.

The load-bearing reliability piece per the OPS-197 plan. Wraps the headless
Claude binary, parses its --output-format stream-json, surfaces the
session_id from the first chunk, and detects Max quota exhaustion from
stderr/exit code so the daemon can throttle.

Cancellation: callers wrap calls in their own asyncio.Task and call
.cancel(); the spawner sends SIGTERM, then SIGKILL after 5s, and reports
status='interrupted' to the ledger.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# Max quota signatures we look for in stderr (strings observed from
# `claude -p` when the rate limit is hit). Conservative — false positives
# here would throttle unnecessarily.
_QUOTA_SIGNATURES = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "5-hour",
    "max plan limit",
)


@dataclass
class StreamChunk:
    """A single parsed JSON object from claude's stream-json stdout."""
    raw: dict[str, Any]

    @property
    def chunk_type(self) -> str | None:
        return self.raw.get("type")

    @property
    def session_id(self) -> str | None:
        # claude emits session_id in init/system chunks; field name varies
        # across CLI versions, so try a couple shapes.
        sid = self.raw.get("session_id") or self.raw.get("id")
        if sid:
            return sid
        # nested under message
        msg = self.raw.get("message") or {}
        return msg.get("id") if isinstance(msg, dict) else None

    @property
    def text_delta(self) -> str | None:
        """Operator-visible text fragment for streaming back to Telegram."""
        # assistant content chunks
        msg = self.raw.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                if parts:
                    return "".join(parts)
        # final result chunk shape
        if self.raw.get("type") == "result":
            return self.raw.get("result")
        return None


@dataclass
class SpawnResult:
    exit_code: int
    claude_session_id: str | None
    full_text: str
    stderr: str
    duration_ms: int
    quota_exhausted: bool
    quota_reset_window: str | None = None
    raw_chunks: list[StreamChunk] = field(default_factory=list)


class Spawner:
    """Bounded-concurrency wrapper for `claude -p`."""

    def __init__(
        self,
        *,
        max_concurrent: int,
        binary: str = "claude",
        timeout_seconds: float = 600.0,
        langfuse_env: dict[str, str] | None = None,
    ):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._binary = binary
        self._timeout = timeout_seconds
        self._in_flight = 0
        # Base Langfuse env injected into every subprocess.  Keys here are
        # lower-priority than the per-call env override so callers can still
        # override individual vars if needed.
        self._langfuse_env: dict[str, str] = langfuse_env or {}

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def spawn(
        self,
        prompt: str,
        *,
        resume_session_id: str | None = None,
        on_chunk: Callable[[StreamChunk], Awaitable[None]] | None = None,
        on_session_id: Callable[[str], Awaitable[None]] | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> SpawnResult:
        """Run `claude -p` once, streaming chunks to on_chunk as they arrive.

        on_session_id fires once when the first stream chunk surfaces the
        session UUID — caller persists that to the ledger so the row is
        recoverable across restarts even if the spawn dies later.
        """
        await self._sem.acquire()
        self._in_flight += 1
        started = time.monotonic()
        try:
            args = [self._binary, "-p", "--output-format", "stream-json", "--verbose"]
            if resume_session_id:
                args += ["--resume", resume_session_id]
            if extra_args:
                args += list(extra_args)

            full_env = os.environ.copy()
            # Inject Langfuse keys first so they are present in every
            # subprocess; per-call env can still override if needed.
            if self._langfuse_env:
                full_env.update(self._langfuse_env)
            if env:
                full_env.update(env)

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )

            # Feed prompt then close stdin so claude knows input is done
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            chunks: list[StreamChunk] = []
            text_parts: list[str] = []
            session_emitted = False

            async def _read_stdout():
                nonlocal session_emitted
                assert proc.stdout is not None
                async for line in proc.stdout:
                    line_s = line.decode("utf-8", errors="replace").strip()
                    if not line_s:
                        continue
                    try:
                        obj = json.loads(line_s)
                    except json.JSONDecodeError:
                        log.debug("non-json stdout line dropped: %r", line_s[:200])
                        continue
                    chunk = StreamChunk(raw=obj)
                    chunks.append(chunk)
                    if (sid := chunk.session_id) and not session_emitted:
                        session_emitted = True
                        if on_session_id:
                            await on_session_id(sid)
                    if (delta := chunk.text_delta):
                        text_parts.append(delta)
                    if on_chunk:
                        await on_chunk(chunk)

            async def _read_stderr() -> str:
                assert proc.stderr is not None
                buf = bytearray()
                async for line in proc.stderr:
                    buf.extend(line)
                return buf.decode("utf-8", errors="replace")

            try:
                stderr_text = await asyncio.wait_for(
                    asyncio.gather(_read_stdout(), _read_stderr(), return_exceptions=False),
                    timeout=self._timeout,
                )
                stderr_str = stderr_text[1]
            except asyncio.TimeoutError:
                log.warning("spawn timeout after %ss; SIGTERM", self._timeout)
                await self._terminate(proc)
                stderr_str = "TIMEOUT after %ss" % self._timeout
            except asyncio.CancelledError:
                log.info("spawn cancelled; SIGTERM")
                await self._terminate(proc)
                raise

            exit_code = await proc.wait()
            quota_hit = _looks_like_quota_exhaustion(stderr_str, exit_code)
            reset_window = _extract_reset_window(stderr_str) if quota_hit else None

            # First-chunk session_id may have been missed if claude died fast;
            # still try to surface anything we caught.
            sid = next((c.session_id for c in chunks if c.session_id), None)

            return SpawnResult(
                exit_code=exit_code,
                claude_session_id=sid,
                full_text="".join(text_parts),
                stderr=stderr_str,
                duration_ms=int((time.monotonic() - started) * 1000),
                quota_exhausted=quota_hit,
                quota_reset_window=reset_window,
                raw_chunks=chunks,
            )
        finally:
            self._in_flight -= 1
            self._sem.release()

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("spawn did not exit on SIGTERM; SIGKILL")
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _looks_like_quota_exhaustion(stderr: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    low = stderr.lower()
    return any(sig in low for sig in _QUOTA_SIGNATURES)


_RESET_RE = re.compile(r"resets?\s+(?:at|in)\s+([0-9TZ:.\- ]+UTC?|\d+\s*(?:min|hour|h|m))", re.IGNORECASE)


def _extract_reset_window(stderr: str) -> str | None:
    m = _RESET_RE.search(stderr)
    return m.group(1).strip() if m else None
