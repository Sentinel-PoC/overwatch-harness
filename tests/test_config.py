"""Settings tests — env file + os.environ override behavior, allowlist parsing.

No Vault calls; secrets module has its own test with mocked hvac.
"""
from __future__ import annotations




def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_TELEGRAM_ALLOWLIST", raising=False)
    from harness.config import HarnessSettings
    s = HarnessSettings(_env_file=None)
    assert s.max_concurrent_sessions == 4
    assert s.idle_window_seconds == 1800
    assert s.healthz_host == "127.0.0.1"
    assert s.allowed_chat_ids() == frozenset()


def test_allowlist_parses_csv(monkeypatch):
    monkeypatch.setenv("HARNESS_TELEGRAM_ALLOWLIST", "12345, 67890 ,11111")
    from harness.config import HarnessSettings
    s = HarnessSettings(_env_file=None)
    assert s.allowed_chat_ids() == frozenset({12345, 67890, 11111})


def test_allowlist_empty_means_fail_closed(monkeypatch):
    monkeypatch.setenv("HARNESS_TELEGRAM_ALLOWLIST", "")
    from harness.config import HarnessSettings
    s = HarnessSettings(_env_file=None)
    assert s.allowed_chat_ids() == frozenset()


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_CONCURRENT_SESSIONS", "8")
    monkeypatch.setenv("HARNESS_IDLE_WINDOW_SECONDS", "60")
    from harness.config import HarnessSettings
    s = HarnessSettings(_env_file=None)
    assert s.max_concurrent_sessions == 8
    assert s.idle_window_seconds == 60


def test_db_path_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from harness.config import HarnessSettings
    s = HarnessSettings(_env_file=None)
    # db_path is computed at class-definition time so it doesn't auto-track HOME;
    # this test pins the contract — db lives under STATE_DIR/harness.db
    assert s.db_path.name == "harness.db"
    assert "overwatch-harness" in str(s.db_path)
