#!/usr/bin/env bash
set -euo pipefail

GATEWAY_TAG="${1:?gateway image is required}"
gateway_smoke="p8-gateway-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
probe_dir="$(mktemp -d)"
http_status=not-probed
curl_status=not-probed
check=container-start

cleanup() {
  status=$?
  if (( status != 0 )); then
    printf 'P8_GATEWAY_SMOKE_FAIL check=%s service=gateway http_status=%s curl_exit=%s\n' \
      "$check" "$http_status" "$curl_status" >&2
    # This isolated gateway has no injected environment, credentials or backend traffic.
    # Select state fields only; never dump Config.Env or a full inspect payload.
    docker inspect --format \
      'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{json .State.Error}}' \
      "$gateway_smoke" >&2 || true
    docker logs --tail 40 "$gateway_smoke" 2>&1 | head -c 8192 >&2 || true
    if [[ -f "$probe_dir/body" ]]; then
      printf '\nhealth response body (first 256 bytes, hex):\n' >&2
      od -An -tx1 -N256 "$probe_dir/body" >&2 || true
    fi
  fi
  docker rm --force "$gateway_smoke" >/dev/null 2>&1 || true
  rm -rf "$probe_dir"
  exit "$status"
}
trap cleanup EXIT

docker run --detach --name "$gateway_smoke" \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /data:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=0700 \
  --tmpfs /config:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=0700 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=1770 \
  "$GATEWAY_TAG" >/dev/null

printf ok > "$probe_dir/expected"
ready=0
deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
  check=container-running
  test "$(docker inspect --format '{{.State.Status}}' "$gateway_smoke")" = running
  check=health-response
  curl_status=0
  http_status="$(docker exec "$gateway_smoke" \
    curl --fail --silent --show-error --max-time 2 \
    --output /tmp/.p8-health-body --write-out '%{http_code}' \
    http://127.0.0.1:8080/healthz)" || curl_status=$?
  docker cp "$gateway_smoke:/tmp/.p8-health-body" "$probe_dir/body" >/dev/null 2>&1 || true
  if [[ "$curl_status" = 0 && "$http_status" = 200 ]] && \
    cmp -s "$probe_dir/expected" "$probe_dir/body"; then
    ready=1
    break
  fi
  sleep 1
done
test "$ready" = 1
check=non-root-user
test "$(docker inspect --format '{{.Config.User}}' "$gateway_smoke")" = 10000:10000
test "$(docker exec "$gateway_smoke" id -u)" = 10000
check=writable-runtime-directories
docker exec "$gateway_smoke" sh -c \
  'touch /data/.p8-smoke /config/.p8-smoke /tmp/.p8-smoke && rm -f /data/.p8-smoke /config/.p8-smoke /tmp/.p8-smoke /tmp/.p8-health-body'
echo 'P8 gateway runtime smoke: PASS'
