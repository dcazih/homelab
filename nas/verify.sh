#!/usr/bin/env bash
set -euo pipefail
host=${1:-192.168.1.152}
check() {
  local label=$1 port=$2 path=$3
  local code
  code=$(curl --silent --show-error --max-time 8 --output /dev/null --write-out '%{http_code}' "http://$host:$port$path")
  if [[ "$code" != 200 && "$code" != 302 ]]; then
    printf '%s failed: HTTP %s\n' "$label" "$code" >&2
    return 1
  fi
  printf '%s: HTTP %s\n' "$label" "$code"
}
check Navidrome 4533 /
check DroppedNeedle 8688 /
check Profiles 8765 /health
check Proxy 8787 /health
check Sona 8790 /health
check Muse 8788 /health
