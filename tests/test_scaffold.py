"""Smoke tests for the v0.1 scaffold itself.

Real test suite (test_router, test_spawner, test_ledger, test_selftest)
lands when the corresponding modules land per OPS-197.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import harness


def test_package_importable():
    """Scaffold imports without error."""
    assert harness.__version__ == "0.1.0.dev0"


def test_main_exits_78_when_vault_unreachable(tmp_path: Path):
    """When secrets can't be loaded, main returns 78 (EX_CONFIG) so systemd
    surfaces the failure via Restart=on-failure rather than masking it.

    Force the failure by pointing VAULT_ADDR at an unreachable host.
    """
    import os
    env = os.environ.copy()
    env["VAULT_ADDR"] = "https://127.0.0.1:1"  # nothing listens here
    env["VAULT_TOKEN"] = "deliberately-bogus"
    # Run from a tmp HOME so the daemon's env file lookup doesn't hit
    # the operator's real config.
    env["HOME"] = str(tmp_path)
    rc = subprocess.run(
        [sys.executable, "-m", "harness"],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    ).returncode
    assert rc == 78


def test_initial_migration_applies_cleanly(tmp_path: Path):
    """0001_init.sql applies to a fresh sqlite DB and creates the expected tables."""
    repo_root = Path(__file__).resolve().parent.parent
    migration = repo_root / "migrations" / "0001_init.sql"
    assert migration.exists(), f"missing {migration}"

    db_path = tmp_path / "harness-test.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(migration.read_text())
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur}
    finally:
        conn.close()

    expected = {
        "engagement",
        "heartbeat",
        "message",
        "operator",
        "routing_failures",
        "schema_version",
        "session",
    }
    missing = expected - tables
    assert not missing, f"migration failed to create: {missing}"

    # Verify schema_version row landed
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT version FROM schema_version ORDER BY version")
        versions = [row[0] for row in cur]
    finally:
        conn.close()
    assert versions == [1]


def test_systemd_unit_uses_restart_on_failure():
    """Critical reliability check: NOT Restart=always (which masked claude-channels' 944-restart pathology)."""
    repo_root = Path(__file__).resolve().parent.parent
    unit = repo_root / "deploy" / "overwatch-harness.service"
    text = unit.read_text()
    assert "Restart=on-failure" in text
    assert "Restart=always" not in text
