#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform-staging}"
if [[ "${INSTALL_ROOT}" != /srv/trading-platform-staging || "${INSTALL_ROOT}" == *production* ]]; then
  echo "Refusing non-staging install root" >&2
  exit 2
fi

install -d -m 700 "${INSTALL_ROOT}" "${INSTALL_ROOT}/config" \
  "${INSTALL_ROOT}/deployments" "${INSTALL_ROOT}/provider"
install -d -m 755 "${INSTALL_ROOT}/bundle"

write_config() {
  local target="$1"
  local temporary
  temporary="$(mktemp "${INSTALL_ROOT}/config/.config.XXXXXX")"
  cat > "${temporary}"
  chmod 600 "${temporary}"
  mv "${temporary}" "${target}"
}

write_config "${INSTALL_ROOT}/config/market.env" <<'EOF'
MARKET_DATA_PROVIDER=replay
MARKET_SYMBOL=SYNTHETIC
MARKET_CONTRACT=P8-STAGING
MARKET_DB_PATH=/data/staging.sqlite3
MARKET_REPLAY_CSV=/data/synthetic.csv
MARKET_REPLAY_SPEED=8
MARKET_HEARTBEAT_SECONDS=5
MARKET_STALE_AFTER_SECONDS=120
MARKET_HISTORY_DAYS=30
MARKET_HISTORY_LIMIT=50000
MARKET_ALLOWED_ORIGINS=http://127.0.0.1:18080
PLATFORM_ENVIRONMENT=test
MARKET_ACCESS_MODE=disabled
PLATFORM_AUTHORIZATION_MODE=disabled
LIVE_EXECUTION_HEALTH_PATH=/run/tw-quant-execution/health.json
LIVE_SHADOW_ENABLED=false
LIVE_CANARY_ENABLED=false
LIVE_AUTO_ENABLED=false
LIVE_AUTO_PRODUCTION_ACCEPTANCE_PASSED=false
PRIVATE_PROVIDER_WHEEL_SHA256=a5cbd8147bfd517e0297f6e70fdbbf33b63c22ab58e570a35bfdbdfc26993ee5
EOF

write_config "${INSTALL_ROOT}/config/execution.env" <<'EOF'
BROKER_PROVIDER=disabled
LIVE_TRADING_ENABLED=false
LIVE_TRADING_CONFIRMATION=
LIVE_BROKER_READ_ONLY_ENABLED=false
LIVE_CANARY_ENABLED=false
LIVE_AUTO_ENABLED=false
LIVE_POSITION_GUARDIAN_ENABLED=false
LIVE_EXECUTION_DB_PATH=/data/staging.sqlite3
LIVE_EXECUTION_HEALTH_PATH=/run/tw-quant-execution/health.json
LIVE_EXECUTION_HEARTBEAT_SECONDS=5
LIVE_RECONCILIATION_INTERVAL_SECONDS=45
LIVE_RECONCILIATION_TIMEOUT_SECONDS=20
LIVE_RECONCILIATION_STALE_SECONDS=120
LIVE_CALLBACK_QUEUE_SIZE=1024
LIVE_CALLBACK_SHUTDOWN_DRAIN_SECONDS=5
EOF

write_config "${INSTALL_ROOT}/config/gateway.env" <<'EOF'
STAGING_DOMAIN=staging.internal
API_MAX_REQUEST_BODY_BYTES=262144
EOF

if [[ ! -s "${INSTALL_ROOT}/provider/factory" ]]; then
  echo "Owner-only staging provider factory is missing" >&2
  exit 3
fi
chmod 400 "${INSTALL_ROOT}/provider/factory"
chown 10001:10001 "${INSTALL_ROOT}/provider/factory"

cat > "${INSTALL_ROOT}/config/compose.env" <<EOF
STAGING_MARKET_ENV_FILE=${INSTALL_ROOT}/config/market.env
STAGING_EXECUTION_ENV_FILE=${INSTALL_ROOT}/config/execution.env
STAGING_GATEWAY_ENV_FILE=${INSTALL_ROOT}/config/gateway.env
STAGING_PROVIDER_FACTORY_FILE=${INSTALL_ROOT}/provider/factory
STAGING_GATEWAY_PORT=18080
EOF
chmod 600 "${INSTALL_ROOT}/config/compose.env"

for path in "${INSTALL_ROOT}/config"/*.env "${INSTALL_ROOT}/provider/factory"; do
  [[ "$(stat -c '%a' "${path}")" =~ ^(400|600)$ ]]
done

echo "Isolated staging host configuration: PASS"
