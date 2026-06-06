"""Spawner tests — uses fake binaries (python scripts) so we don't depend on
the real `claude` CLI being present, and we control the stream-json shape.

Lives outside the unit-test fast lane only when we deliberately exercise
subprocess; that's the whole point of this module's tests, so we accept it.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from harness.spawner import Spawner, _extract_reset_window, _looks_like_quota_exhaustion


def _write_fake_claude(tmp_path: Path, *, stdout_lines: list[str], stderr: str = "", rc: int = 0) -> Path:
    """Write a python script that mimics `claude -p` for a single run."""
    script = tmp_path / "fake_claude.py"
    payload = {
        "stdout": stdout_lines,
        "stderr": stderr,
        "rc": rc,
    }
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys, time
        payload = {json.dumps(payload)}
        # Drain stdin so `claude -p` semantics match (read a prompt then close).
        try:
            sys.stdin.read()
        except Exception:
            pass
        for line in payload["stdout"]:
            sys.stdout.write(line + "\\n")
            sys.stdout.flush()
        if payload["stderr"]:
            sys.stderr.write(payload["stderr"])
            sys.stderr.flush()
        sys.exit(payload["rc"])
    """))
    script.chmod(0o755)
    # Wrap so it's invokable as `<path>` directly via execve
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.asyncio
async def test_spawn_happy_path_streams_chunks_and_session_id(tmp_path):
    chunks_emitted = [
        json.dumps({"type": "system", "session_id": "abc-123"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi "}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "there"}]}}),
        json.dumps({"type": "result", "result": "hi there"}),
    ]
    binary = _write_fake_claude(tmp_path, stdout_lines=chunks_emitted, rc=0)

    seen_sid: list[str] = []
    seen_chunks: list = []

    async def _on_sid(sid):
        seen_sid.append(sid)

    async def _on_chunk(c):
        seen_chunks.append(c)

    s = Spawner(max_concurrent=2, binary=str(binary))
    res = await s.spawn("hello", on_chunk=_on_chunk, on_session_id=_on_sid)

    assert res.exit_code == 0
    assert res.claude_session_id == "abc-123"
    assert seen_sid == ["abc-123"]
    assert "hi there" in res.full_text
    assert len(seen_chunks) == 4
    assert res.quota_exhausted is False


@pytest.mark.asyncio
async def test_spawn_quota_signature_in_stderr_flags_exhaustion(tmp_path):
    binary = _write_fake_claude(
        tmp_path,
        stdout_lines=[json.dumps({"type": "system", "session_id": "x"})],
        stderr="Error: rate limit reached for max plan; resets at 2026-04-30T05:00:00Z UTC\n",
        rc=1,
    )
    s = Spawner(max_concurrent=1, binary=str(binary))
    res = await s.spawn("x")
    assert res.exit_code == 1
    assert res.quota_exhausted is True
    assert res.quota_reset_window is not None and "2026" in res.quota_reset_window


@pytest.mark.asyncio
async def test_spawn_clean_exit_no_false_quota_positive(tmp_path):
    # Exit 0, no signature → not quota exhaustion even if stderr has noise.
    binary = _write_fake_claude(
        tmp_path,
        stdout_lines=[json.dumps({"type": "result", "result": "ok"})],
        stderr="warn: deprecation notice\n",
        rc=0,
    )
    s = Spawner(max_concurrent=1, binary=str(binary))
    res = await s.spawn("x")
    assert res.quota_exhausted is False


@pytest.mark.asyncio
async def test_spawn_concurrency_bounded(tmp_path):
    # Slow fake — sleeps 0.5s before exit. With max_concurrent=1, two
    # concurrent spawns must serialize.
    script = tmp_path / "slow_claude.py"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, time, json
        try:
            sys.stdin.read()
        except Exception: pass
        time.sleep(0.5)
        sys.stdout.write(json.dumps({"type":"result","result":"ok"}) + "\\n")
        sys.exit(0)
    """))
    script.chmod(0o755)
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)

    s = Spawner(max_concurrent=1, binary=str(wrapper))
    import time as _t
    t0 = _t.monotonic()
    await asyncio.gather(s.spawn("a"), s.spawn("b"))
    elapsed = _t.monotonic() - t0
    # Two sequential 0.5s spawns ≥ ~1s; if they ran in parallel we'd see ~0.5s.
    assert elapsed >= 0.9, f"expected serialization, got {elapsed:.2f}s"


def test_quota_signature_unit():
    assert _looks_like_quota_exhaustion("rate limit reached", 1)
    assert _looks_like_quota_exhaustion("Max plan limit hit", 1)
    assert not _looks_like_quota_exhaustion("ok", 0)
    assert not _looks_like_quota_exhaustion("transient network error", 1)


def test_reset_window_extraction():
    assert _extract_reset_window("rate limit; resets at 2026-04-30T05:00:00Z UTC") is not None
    assert _extract_reset_window("rate limit; resets in 32 minutes") is not None
    assert _extract_reset_window("no signature here") is None
