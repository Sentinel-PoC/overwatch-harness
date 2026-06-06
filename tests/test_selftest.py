"""Selftest + state machine tests."""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from harness.selftest import DaemonState, StateMachine, run_selftest


def _write_fake_claude(tmp_path: Path, *, version_rc: int = 0,
                      spawn_chunks: list[str] | None = None,
                      spawn_rc: int = 0,
                      version_stderr: str = "") -> Path:
    """Create a fake `claude` shim that handles --version and -p modes."""
    payload = {
        "version_rc": version_rc,
        "version_stderr": version_stderr,
        "spawn_chunks": spawn_chunks or [],
        "spawn_rc": spawn_rc,
    }
    script = tmp_path / "fake_claude_shim.py"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        payload = {json.dumps(payload)}
        if "--version" in sys.argv:
            if payload["version_stderr"]:
                sys.stderr.write(payload["version_stderr"])
            print("claude version 0.0.0-test")
            sys.exit(payload["version_rc"])
        # spawn mode
        try:
            sys.stdin.read()
        except Exception: pass
        for line in payload["spawn_chunks"]:
            sys.stdout.write(line + "\\n")
            sys.stdout.flush()
        sys.exit(payload["spawn_rc"])
    """))
    script.chmod(0o755)
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.asyncio
async def test_selftest_happy_path(tmp_path):
    chunks = [json.dumps({"type": "system", "session_id": "x"}),
              json.dumps({"type": "result", "result": "ok"})]
    bin_ = _write_fake_claude(tmp_path, spawn_chunks=chunks)
    res = await run_selftest(binary=str(bin_))
    assert res.healthy is True
    assert res.version_ok is True and res.spawn_ok is True


@pytest.mark.asyncio
async def test_selftest_binary_missing():
    res = await run_selftest(binary="/nonexistent/claude/binary")
    assert res.healthy is False
    assert res.version_ok is False
    assert "not found" in res.notes


@pytest.mark.asyncio
async def test_selftest_version_failure_short_circuits(tmp_path):
    bin_ = _write_fake_claude(tmp_path, version_rc=1, version_stderr="oauth missing")
    res = await run_selftest(binary=str(bin_))
    assert res.healthy is False and res.version_ok is False and res.spawn_ok is False
    assert "oauth" in res.notes.lower()


@pytest.mark.asyncio
async def test_selftest_spawn_failure_after_version_ok(tmp_path):
    bin_ = _write_fake_claude(tmp_path, version_rc=0, spawn_chunks=[], spawn_rc=2)
    res = await run_selftest(binary=str(bin_))
    assert res.version_ok is True
    assert res.spawn_ok is False
    assert res.healthy is False


@pytest.mark.asyncio
async def test_state_machine_starts_in_starting():
    sm = StateMachine()
    assert sm.state == DaemonState.STARTING
    assert sm.can_spawn is False


@pytest.mark.asyncio
async def test_state_machine_healthy_allows_spawn():
    sm = StateMachine()
    await sm.transition(DaemonState.HEALTHY, reason="selftest passed")
    assert sm.can_spawn is True


@pytest.mark.asyncio
async def test_state_machine_degraded_refuses_spawn():
    sm = StateMachine()
    await sm.transition(DaemonState.DEGRADED, reason="selftest failed")
    assert sm.can_spawn is False
    assert sm.reason == "selftest failed"


@pytest.mark.asyncio
async def test_state_machine_degraded_to_healthy_blocked():
    sm = StateMachine()
    await sm.transition(DaemonState.DEGRADED, reason="x")
    await sm.transition(DaemonState.HEALTHY, reason="trying to recover")
    assert sm.state == DaemonState.DEGRADED  # refused
