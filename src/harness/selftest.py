"""Startup self-test + degraded state machine.

The whole reason this daemon exists separately from claude-channels is
that claude-channels masked an unrunnable claude binary by infinite-
restarting. We refuse to make that mistake: BEFORE accepting Telegram
traffic, verify `claude -p --version` returns and `claude -p "ok"`
streams a result chunk within timeout. If either fails, the daemon
enters DEGRADED — still acks Telegram (so the operator notices) but
refuses spawns until restart.

Exit-degraded only on operator-driven restart. We deliberately do NOT
auto-recover; recovery requires human attention to whatever broke
claude in the first place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class DaemonState(str, Enum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    QUOTA_THROTTLED = "quota_throttled"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True, slots=True)
class SelftestResult:
    healthy: bool
    version_ok: bool
    spawn_ok: bool
    notes: str


async def run_selftest(
    *,
    binary: str = "claude",
    timeout_seconds: float = 30.0,
    env: dict[str, str] | None = None,
) -> SelftestResult:
    """Two-phase check: --version, then a real spawn returning 'ok'.

    Failure of either phase → DEGRADED. We capture stderr so the operator
    can see *why* it failed (binary not found / OAuth missing / etc).
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    notes_parts: list[str] = []

    # Phase 1: version
    version_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode == 0:
            version_ok = True
            notes_parts.append(f"version: {out.decode('utf-8', 'replace').strip()[:60]}")
        else:
            notes_parts.append(
                f"--version exit={proc.returncode} stderr={err.decode('utf-8', 'replace').strip()[:200]}"
            )
    except FileNotFoundError:
        notes_parts.append(f"binary {binary!r} not found on PATH")
    except asyncio.TimeoutError:
        notes_parts.append("--version timed out after 10s")

    if not version_ok:
        return SelftestResult(healthy=False, version_ok=False, spawn_ok=False,
                              notes=" | ".join(notes_parts))

    # Phase 2: spawn 'ok' and look for any stream-json result chunk
    spawn_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-p", "--output-format", "stream-json", "--verbose",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"ok")
        await proc.stdin.drain()
        proc.stdin.close()

        async def _drain():
            assert proc.stdout is not None
            assert proc.stderr is not None
            saw_result = False
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("result", "assistant"):
                    saw_result = True
            err = await proc.stderr.read()
            return saw_result, err

        saw_result, err = await asyncio.wait_for(_drain(), timeout=timeout_seconds)
        rc = await proc.wait()
        if rc == 0 and saw_result:
            spawn_ok = True
            notes_parts.append("spawn 'ok': result chunk received")
        else:
            notes_parts.append(
                f"spawn exit={rc} saw_result={saw_result} stderr={err.decode('utf-8', 'replace').strip()[:200]}"
            )
    except asyncio.TimeoutError:
        notes_parts.append(f"spawn 'ok' timed out after {timeout_seconds}s")
        try:
            proc.kill()
        except Exception:
            pass
    except FileNotFoundError:
        notes_parts.append(f"binary {binary!r} disappeared between phases")

    healthy = version_ok and spawn_ok
    return SelftestResult(
        healthy=healthy,
        version_ok=version_ok,
        spawn_ok=spawn_ok,
        notes=" | ".join(notes_parts),
    )


class StateMachine:
    """Mutable state holder for the daemon's runtime mode.

    Transitions are deliberately one-way unless explicitly reset.
    DEGRADED never transitions to HEALTHY without operator restart.
    """

    def __init__(self):
        self._state = DaemonState.STARTING
        self._reason: str | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def can_spawn(self) -> bool:
        return self._state == DaemonState.HEALTHY

    async def transition(self, new: DaemonState, *, reason: str | None = None) -> None:
        async with self._lock:
            if self._state == DaemonState.DEGRADED and new == DaemonState.HEALTHY:
                # Refuse: degraded → healthy only on restart.
                log.warning("refusing degraded→healthy transition; restart required")
                return
            log.info("state %s → %s (%s)", self._state.value, new.value, reason or "")
            self._state = new
            self._reason = reason
