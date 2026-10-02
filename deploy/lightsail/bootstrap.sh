#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="${1:?public repository URL is required}"
DEPLOY_BRANCH="${2:-master}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/lightsail/bootstrap.sh" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 git ca-certificates curl
systemctl enable --now docker

install -d -m 0755 "${INSTALL_ROOT}" "${INSTALL_ROOT}/config"
install -d -m 0700 "${INSTALL_ROOT}/secrets"
if [[ ! -d "${INSTALL_ROOT}/repo/.git" ]]; then
  git clone --branch "${DEPLOY_BRANCH}" "${REPOSITORY_URL}" "${INSTALL_ROOT}/repo"
fi

for file in market gateway execution compose; do
  target="${INSTALL_ROOT}/config/${file}.env"
  source="${INSTALL_ROOT}/repo/deploy/lightsail/${file}.env.example"
  if [[ ! -e "${target}" ]]; then
    install -m 0600 "${source}" "${target}"
  fi
done

echo "Bootstrap complete. Configure external environment files and credentials."
echo "Deployment requires a manual exact-revision CI/Security-verified request."
