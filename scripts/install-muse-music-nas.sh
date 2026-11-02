#!/usr/bin/env bash
# Deploy only the existing Music Profiles MCP connector for Meta Muse.
# Does not modify the NAS administrator MCP, its port, or its root OAuth scopes.
set -Eeuo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer with sudo.' >&2
  exit 1
fi

NAS_IP='192.168.1.152'
SOURCE='/mnt/Pool/music-server/source'
STATE='/mnt/Pool/music-server/state/muse'
PRIVATE='/mnt/Pool/music-server/private/muse.env'
COMPOSE="$SOURCE/nas/compose.yml"
OVERRIDE="$STATE/muse-public.override.yml"
TAILSCALE_CONTAINER='ix-tailscale-tailscale-1'
PUBLIC_URL='https://homelab.hippocampus-polaris.ts.net:8443'
SOURCE_REF="${MUSE_MUSIC_SOURCE_REF:-main}"
if [ -z "$SOURCE_REF" ]; then SOURCE_REF='main'; fi

for program in curl docker python3; do
  command -v "$program" >/dev/null || { echo "Missing $program" >&2; exit 1; }
done
docker compose version >/dev/null
docker inspect "$TAILSCALE_CONTAINER" >/dev/null 2>&1 || {
  echo 'The TrueNAS Tailscale app container is missing.' >&2
  exit 1
}
[ -f "$COMPOSE" ] || { echo "Missing $COMPOSE" >&2; exit 1; }
[ -f "$PRIVATE" ] || { echo "Missing private Muse env file $PRIVATE" >&2; exit 1; }
[ -d "$SOURCE/muse-music-connector" ] || {
  echo 'Missing existing Muse connector source folder.' >&2
  exit 1
}
if ! grep -q '^STAGE2_ENCRYPTION_KEY=' "$PRIVATE"; then
  echo 'The existing Muse encryption key is missing; refusing to replace the service.' >&2
  exit 1
fi
if ! curl --fail --silent --show-error --max-time 8 "http://$NAS_IP:8765/health" >/dev/null; then
  echo 'Music Profiles Bridge is not reachable on port 8765. No changes made.' >&2
  exit 1
fi

# Keep existing /data (OAuth records and encrypted profile sessions) intact.
mkdir -p "$STATE"
TMP="$(mktemp -d /tmp/muse-music-fix.XXXXXXXX)"
trap 'rm -rf "$TMP"' EXIT
RAW="https://raw.githubusercontent.com/dcazih/homelab/$SOURCE_REF/muse-music-connector"
curl --fail --silent --show-error --location --retry 3 "$RAW/server.py" -o "$TMP/server.py"
python3 -m py_compile "$TMP/server.py"
grep -q "MUSE_CLIENT_ID = 'muse'" "$TMP/server.py" || {
  echo 'Downloaded code lacks the built-in Muse OAuth client.' >&2
  exit 1
}
cp -p "$SOURCE/muse-music-connector/server.py" "$TMP/server.py.before"
install -m 0644 "$TMP/server.py" "$SOURCE/muse-music-connector/server.py"

# Only add an override for the public music origin. Do not rewrite the
# existing TrueNAS compose file or overwrite private credentials.
cat > "$OVERRIDE" <<EOF
services:
  muse-music:
    environment:
      STAGE2_PUBLIC_URL: "$PUBLIC_URL"
EOF

echo 'Building and updating ONLY homelab-music muse-music...'
docker compose -f "$COMPOSE" -f "$OVERRIDE" config -q
docker compose -f "$COMPOSE" -f "$OVERRIDE" up -d --build --no-deps muse-music

LOCAL="http://$NAS_IP:8788"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent --show-error --max-time 3 "$LOCAL/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error --max-time 5 "$LOCAL/health" >/dev/null

# Validate OAuth discovery AND the pre-registered Meta callback, rather than
# relying on a 401 or an apparently healthy container.
curl --fail --silent --show-error --max-time 5 "$LOCAL/.well-known/oauth-authorization-server" | python3 -c '
import json, sys
metadata=json.load(sys.stdin)
assert metadata["issuer"] == "https://homelab.hippocampus-polaris.ts.net:8443"
assert metadata["authorization_endpoint"] == metadata["issuer"]+"/authorize"
assert metadata["token_endpoint"] == metadata["issuer"]+"/token"
assert "none" in metadata["token_endpoint_auth_methods_supported"]
assert "S256" in metadata["code_challenge_methods_supported"]
assert set(metadata["scopes_supported"]) == {"music:read","music:write"}
'
AUTH_CODE="$(curl --silent --show-error --max-time 5 --get --output "$TMP/authorize.html" --write-out '%{http_code}' \
  --data-urlencode 'client_id=muse' \
  --data-urlencode 'redirect_uri=https://agent.meta.ai/api/hatch/oauth/callback' \
  --data-urlencode 'response_type=code' \
  --data-urlencode 'code_challenge_method=S256' \
  --data-urlencode 'code_challenge=0123456789abcdefghijklmnopqrstuvwxyzABCDEFG' \
  --data-urlencode 'scope=music:read music:write' \
  --data-urlencode 'state=muse-music-smoke' \
  "$LOCAL/authorize")"
if [ "$AUTH_CODE" != '200' ] || ! grep -q 'Authorize Muse' "$TMP/authorize.html"; then
  echo "Meta Muse OAuth authorization preflight failed (HTTP $AUTH_CODE)." >&2
  echo 'Existing music database is preserved. Check docker compose logs muse-music.' >&2
  exit 1
fi

MCP_CODE="$(curl --silent --show-error --max-time 5 --output /dev/null --write-out '%{http_code}' "$LOCAL/mcp" || true)"
if [ "$MCP_CODE" != '401' ]; then
  echo "MCP unauthenticated discovery should return 401; got $MCP_CODE" >&2
  exit 1
fi

# Tailscale Funnel permits HTTPS port 8443. This coexists with the existing
# port 443 NAS admin Funnel without remapping the admin endpoint.
echo 'Music OAuth and PKCE preflight passed. Publishing music on HTTPS :8443.'
docker exec "$TAILSCALE_CONTAINER" tailscale funnel --bg --https=8443 "$LOCAL"

echo
echo 'Muse Music MCP endpoint:'
echo "$PUBLIC_URL/mcp"
echo 'Muse OAuth client ID: muse'
echo 'Muse callback: https://agent.meta.ai/api/hatch/oauth/callback'
echo 'OAuth scopes: music:read music:write'
echo 'Sign in on the authorization page with your enrolled Navidrome account.'
echo 'The NAS root MCP gateway was not modified.'
