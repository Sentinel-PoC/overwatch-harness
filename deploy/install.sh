#!/usr/bin/env bash
# install.sh — Set up overwatch-harness on the operator workstation.
#
# Usage: ./deploy/install.sh
#   Run as the operator (koiakoia), NOT root. The daemon runs in user-systemd
#   and reads ~/.claude/.credentials.json for Claude Max OAuth. A system-level
#   install would lose that credential path.
#
# Idempotent: re-running upgrades the venv + relinks the systemd unit.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${HOME}/.local/share/overwatch-harness/venv"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/overwatch-harness"
STATE_DIR="${HOME}/.local/state/overwatch-harness"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "=== overwatch-harness install ==="
echo "Repo:        ${REPO_DIR}"
echo "Venv:        ${VENV_DIR}"
echo "Config:      ${CONFIG_DIR}"
echo "State:       ${STATE_DIR}"
echo

# Refuse to run as root — daemon must run as operator user to reach Claude OAuth
if [[ "$(id -u)" -eq 0 ]]; then
    echo "ERROR: install.sh must NOT be run as root."
    echo "Daemon needs operator's ~/.claude/.credentials.json for Max auth."
    exit 1
fi

mkdir -p "${VENV_DIR%/venv}" "${BIN_DIR}" "${CONFIG_DIR}" "${STATE_DIR}" "${SYSTEMD_DIR}"

# Venv + dep install
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "-> creating venv"
    python3 -m venv "${VENV_DIR}"
fi

echo "-> installing/upgrading deps from pyproject.toml"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"

# Wrapper bin
cat > "${BIN_DIR}/overwatch-harness" <<EOF
#!/usr/bin/env bash
exec ${VENV_DIR}/bin/overwatch-harness "\$@"
EOF
chmod +x "${BIN_DIR}/overwatch-harness"
echo "-> wrote wrapper at ${BIN_DIR}/overwatch-harness"

# systemd-user unit
ln -sf "${REPO_DIR}/deploy/overwatch-harness.service" "${SYSTEMD_DIR}/overwatch-harness.service"
echo "-> linked systemd unit"

systemctl --user daemon-reload

# Initial schema apply (idempotent — IF NOT EXISTS in migrations)
if [[ -f "${REPO_DIR}/migrations/0001_init.sql" ]]; then
    DB_PATH="${STATE_DIR}/harness.db"
    if [[ ! -f "${DB_PATH}" ]]; then
        echo "-> applying initial schema to ${DB_PATH}"
        sqlite3 "${DB_PATH}" < "${REPO_DIR}/migrations/0001_init.sql"
    else
        echo "-> ledger db already exists at ${DB_PATH}; skipping init"
    fi
fi

# Skeleton env file if not present
if [[ ! -f "${CONFIG_DIR}/env" ]]; then
    cat > "${CONFIG_DIR}/env" <<'EOF'
# overwatch-harness environment
# Secrets resolved from Vault at startup via hvac. Only non-secret config here.

# Vault address (direct VM IP — survives cluster outage)
VAULT_ADDR=https://192.168.12.206:8200
VAULT_SKIP_VERIFY=true

# Path-style references; daemon reads actual values from these Vault KV paths
HARNESS_VAULT_TELEGRAM_TOKEN_PATH=secret/data/overwatch-harness/telegram
HARNESS_VAULT_PLANE_KEY_PATH=secret/data/plane/api-key
HARNESS_VAULT_LANGFUSE_PATH=secret/data/langfuse/overwatch-agents

# Plane workspace + project
HARNESS_PLANE_WORKSPACE=haists-it-consulting
HARNESS_PLANE_PROJECT_ID=

# Tunables
HARNESS_MAX_CONCURRENT_SESSIONS=4
HARNESS_IDLE_WINDOW_SECONDS=1800
HARNESS_LOG_LEVEL=INFO

# Allowlist (comma-separated Telegram chat_ids; populate before enabling)
HARNESS_TELEGRAM_ALLOWLIST=
EOF
    chmod 0600 "${CONFIG_DIR}/env"
    echo "-> created skeleton env at ${CONFIG_DIR}/env (mode 0600)"
    echo "   EDIT THIS FILE before enabling the service."
fi

echo
echo "=== install complete ==="
echo
echo "Next steps:"
echo "  1. Edit ${CONFIG_DIR}/env (Plane project id, Telegram chat_id allowlist)"
echo "  2. Cutover from claude-channels:"
echo "       systemctl --user disable --now claude-channels.service"
echo "       systemctl --user enable --now overwatch-harness.service"
echo "  3. Watch: journalctl --user -u overwatch-harness.service -f"
echo
echo "Rollback path:"
echo "       systemctl --user disable --now overwatch-harness.service"
echo "       systemctl --user enable --now claude-channels.service"
