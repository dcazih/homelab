#!/usr/bin/env bash
# Update the EXISTING Muse Music MCP container in place. No Compose files needed.
# Does not modify TrueNAS root-admin MCP, Bridge databases, or music services.
set -Eeuo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo.' >&2
  exit 1
fi

CONTAINER='muse-music'
TAILSCALE_CONTAINER='ix-tailscale-tailscale-1'
NAS_IP='192.168.1.152'
PUBLIC_URL='https://homelab.hippocampus-polaris.ts.net:8443'
MUSE_REF="${MUSE_MUSIC_SOURCE_REF:-main}"

for cmd in curl docker python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing $cmd" >&2; exit 1; }
done
# Verify the Bridge before creating the connector. The NAS administrator
# gateway and all existing music containers remain untouched.
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo 'Muse Music container missing. Preparing a NEW music-only connector.'
  curl --fail --silent --show-error --max-time 8 "http://$NAS_IP:8765/health" >/dev/null || {
    echo 'Bridge port 8765 is unreachable. Nothing installed.' >&2
    exit 1
  }
  if docker ps --format '{{.Names}} {{.Ports}}' | grep -E '(^|[ :])([0-9.]+:)?8788->' ; then
    echo 'Port 8788 is occupied by another container. No changes made.' >&2
    exit 1
  fi
  if command -v ss >/dev/null 2>&1 && ss -ltnH | awk '{print $4}' | grep -qE ':8788 >/dev/null 2>&1 || {
  echo "Tailscale app container was not found. No changes made." >&2
  exit 1
}
docker exec "$CONTAINER" test -f /app/server.py || {
  echo 'Existing container has no /app/server.py. No changes made.' >&2
  exit 1
}
python3 - "$CONTAINER" <<'PY'
import json, subprocess, sys
name=sys.argv[1]
data=json.loads(subprocess.check_output(['docker','inspect',name], text=True))[0]
if data.get('HostConfig',{}).get('ReadonlyRootfs'):
    raise SystemExit('Container root filesystem is read-only; refusing in-place update.')
mounts=data.get('Mounts',[])
if not any(m.get('Destination')=='/data' for m in mounts):
    raise SystemExit('Existing Muse /data mount missing; refusing to touch state.')
if not any(x.startswith('STAGE2_ENCRYPTION_KEY=') and x.split('=',1)[1]
           for x in data.get('Config',{}).get('Env',[])):
    raise SystemExit('Existing Muse encryption key missing; refusing update.')
ports=data.get('NetworkSettings',{}).get('Ports',{}).get('8788/tcp') or []
if not any(str(p.get('HostPort'))=='8788' and p.get('HostIp') in ('192.168.1.152','0.0.0.0','')
           for p in ports):
    raise SystemExit('Muse is not published at NAS port 8788; no changes made.')
print('Existing Muse container, encryption key, /data mount, and port verified.')
PY

curl --fail --silent --show-error --max-time 6 "http://$NAS_IP:8765/health" >/dev/null || {
  echo 'Music Profiles Bridge on port 8765 is unavailable. No changes made.' >&2
  exit 1
}

umask 077
TEMP="$(mktemp -d /tmp/muse-connector-patch.XXXXXXXX)"
MODIFIED=0
ORIGIN_PRESENT=0

restore_on_error() {
  code="$?"
  trap - ERR
  if [ "$MODIFIED" = 1 ]; then
    echo 'Update failed. Restoring previous Muse connector code and public origin.' >&2
    docker cp "$TEMP/server.original.py" "$CONTAINER:/app/server.py" || true
    if [ "$ORIGIN_PRESENT" = 1 ]; then
      docker cp "$TEMP/origin.original" "$CONTAINER:/data/muse-public-origin" || true
    else
      docker exec -u 0 "$CONTAINER" rm -f /data/muse-public-origin || true
    fi
    docker restart "$CONTAINER" >/dev/null || true
  fi
  exit "$code"
}
cleanup() { rm -rf -- "$TEMP"; }
trap restore_on_error ERR
trap cleanup EXIT

docker cp "$CONTAINER:/app/server.py" "$TEMP/server.original.py"
if docker exec "$CONTAINER" test -f /data/muse-public-origin; then
  ORIGIN_PRESENT=1
  docker cp "$CONTAINER:/data/muse-public-origin" "$TEMP/origin.original"
fi

RAW="https://raw.githubusercontent.com/dcazih/homelab/$MUSE_REF/muse-music-connector/server.py"
curl --fail --silent --show-error --location --retry 3 "$RAW" -o "$TEMP/server.py"
python3 -m py_compile "$TEMP/server.py"
grep -q "MUSE_CLIENT_ID = 'muse'" "$TEMP/server.py"
grep -q "PUBLIC_ORIGIN_OVERRIDE = Path('/data/muse-public-origin')" "$TEMP/server.py"

# The existing container may carry an old OAuth issuer in its environment.
# /data is a persistent volume and takes priority over the old environment.
printf '%s\n' "$PUBLIC_URL" > "$TEMP/muse-public-origin"
chmod 0644 "$TEMP/muse-public-origin" "$TEMP/server.py"

MODIFIED=1
docker cp "$TEMP/muse-public-origin" "$CONTAINER:/data/muse-public-origin"
docker cp "$TEMP/server.py" "$CONTAINER:/app/server.py"
docker restart "$CONTAINER" >/dev/null

LOCAL="http://$NAS_IP:8788"
READY=0
for n in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent --show-error --max-time 3 "$LOCAL/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" != 1 ]; then
  echo 'Updated Muse did not become healthy.' >&2
  docker logs --tail 35 "$CONTAINER" >&2 || true
  false
fi

# Check OAuth discovery, authorization page, and unauthorized MCP challenge.
curl --fail --silent --show-error --max-time 6 \
  "$LOCAL/.well-known/oauth-authorization-server" | python3 -c '
import json,sys
data=json.load(sys.stdin)
assert data["issuer"]=="https://homelab.hippocampus-polaris.ts.net:8443"
assert data["token_endpoint_auth_methods_supported"]==["none"]
assert "S256" in data["code_challenge_methods_supported"]
assert set(data["scopes_supported"])=={"music:read","music:write"}
'
CODE="$(curl --silent --show-error --max-time 6 --get \
  --output "$TEMP/auth.html" --write-out '%{http_code}' \
  --data-urlencode 'client_id=muse' \
  --data-urlencode 'redirect_uri=https://agent.meta.ai/api/hatch/oauth/callback' \
  --data-urlencode 'response_type=code' \
  --data-urlencode 'code_challenge_method=S256' \
  --data-urlencode 'code_challenge=0123456789abcdefghijklmnopqrstuvwxyzABCDEFG' \
  --data-urlencode 'scope=music:read music:write' \
  --data-urlencode 'state=connector-preflight' \
  "$LOCAL/authorize")"
if [ "$CODE" != 200 ] || ! grep -q 'Authorize Muse' "$TEMP/auth.html"; then
  echo "Muse music authorization preflight failed (HTTP $CODE)." >&2
  false
fi
CODE="$(curl --silent --show-error --max-time 6 --output /dev/null --write-out '%{http_code}' "$LOCAL/mcp")"
if [ "$CODE" != 401 ]; then
  echo "Muse MCP endpoint must challenge with 401, got HTTP $CODE." >&2
  false
fi
MODIFIED=0
echo 'Music OAuth and MCP preflight passed. Existing data and settings preserved.'

# Also update the local source tree if present, so the corrected connector
# survives a future Compose rebuild. The running container does not depend on
# the source tree being present. Keep a timestamped backup in that folder.
HOST_SOURCE='/mnt/Pool/music-server/source/muse-music-connector/server.py'
if [ -f "$HOST_SOURCE" ]; then
  HOST_BACKUP="$HOST_SOURCE.before-muse-fix.$(date +%Y%m%d%H%M%S)"
  if cp -p "$HOST_SOURCE" "$HOST_BACKUP" &&
     install -m 0644 "$TEMP/server.py" "$HOST_SOURCE"; then
    echo "Persistent source patched; backup: $HOST_BACKUP"
  else
    echo 'WARNING: running container is updated, but host source persistence failed.' >&2
  fi
else
  echo 'WARNING: no host source file found; a future container recreation could revert this patch.' >&2
fi

# This is a separate Funnel listener on :8443, not the existing root-admin
# listener on :443. Do not alter the NAS gateway or tailnet-only services.
docker exec "$TAILSCALE_CONTAINER" tailscale funnel --bg --https=8443 "$LOCAL"
echo 'Checking public music OAuth metadata...'
PUBLIC_READY=0
for n in 1 2 3 4 5; do
  if curl --fail --silent --show-error --max-time 8 \
       "$PUBLIC_URL/.well-known/oauth-authorization-server" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["issuer"]=="https://homelab.hippocampus-polaris.ts.net:8443"
assert set(d.get("scopes_supported",[]))=={"music:read","music:write"}
' >/dev/null 2>&1; then
    PUBLIC_READY=1
    break
  fi
  sleep 2
done
echo
echo "Muse Music MCP address: $PUBLIC_URL/mcp"
echo 'Muse client ID: muse'
echo 'Client secret: none'
echo 'Permissions: music:read music:write (NOT nas:root)'
echo 'Authorize with your enrolled Navidrome username and password.'
if [ "$PUBLIC_READY" = 1 ]; then
  echo 'Public music OAuth metadata verified.'
else
  echo 'WARNING: public Funnel metadata could not be verified from this NAS.' >&2
  echo "Verify $PUBLIC_URL/.well-known/oauth-authorization-server before retrying Muse." >&2
fi
echo 'Your NAS administrator gateway was not changed.'
; then
    echo 'Port 8788 is already in use. No changes made.' >&2
    exit 1
  fi
  [ -d /mnt/Pool ] || {
    echo 'TrueNAS pool /mnt/Pool is missing; refusing to create data on OS disk.' >&2
    exit 1
  }
  SOURCE_DIR='/mnt/Pool/music-server'
  DATA_DIR="$SOURCE_DIR/state/muse"
  ENV_FILE="$SOURCE_DIR/private/muse.env"
  mkdir -p "$SOURCE_DIR/private" "$DATA_DIR"
  chmod 700 "$SOURCE_DIR/private" "$DATA_DIR"
  if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$DATA_DIR/stage2.sqlite3" ]; then
      echo 'Existing Muse OAuth database found without encryption key. Refusing to overwrite.' >&2
      exit 1
    fi
    echo 'Creating a dedicated encrypted Muse credential store (never shared over SMB).'
    python3 - "$ENV_FILE" <<'PY'
import base64, os, sys
from pathlib import Path
p=Path(sys.argv[1])
key=base64.urlsafe_b64encode(os.urandom(32)).decode()
fd=os.open(str(p),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as fp:
    fp.write('STAGE2_ENCRYPTION_KEY='+key+'\n')
PY
  fi
  chmod 600 "$ENV_FILE"
  if ! grep -q '^STAGE2_ENCRYPTION_KEY=.' "$ENV_FILE"; then
    echo 'Muse encryption key missing in private env file; refusing to overwrite it.' >&2
    exit 1
  fi
  BUILD_DIR="$(mktemp -d /tmp/muse-music-build.XXXXXXXX)"
  RAW="https://raw.githubusercontent.com/dcazih/homelab/$MUSE_REF/muse-music-connector"
  for item in Dockerfile requirements.txt server.py; do
    curl --fail --silent --show-error --location --retry 3 \
      "$RAW/$item" -o "$BUILD_DIR/$item" || {
        echo "Could not download $item. Nothing installed." >&2
        rm -rf "$BUILD_DIR"
        exit 1
      }
  done
  python3 -m py_compile "$BUILD_DIR/server.py"
  echo 'Building the Muse Music connector image from the pinned GitHub revision...'
  if ! docker build -t homelab/muse-music:oauth -f "$BUILD_DIR/Dockerfile" "$BUILD_DIR"; then
    rm -rf "$BUILD_DIR"
    echo 'Docker build failed; no container created.' >&2
    exit 1
  fi
  rm -rf "$BUILD_DIR"
  # The existing homelab NAS Compose uses UID/GID 3000 for access to music
  # datasets. Bind only the private Muse state path; Bridge is reached through
  # its existing published host endpoint, without exposing Bridge publicly.
  chown 3000:3000 "$DATA_DIR"
  echo 'Starting dedicated Muse Music connector on NAS port 8788...'
  docker run --detach \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --cap-drop ALL --security-opt no-new-privileges \
    --user 3000:3000 \
    --publish "$NAS_IP:8788:8788" \
    --env-file "$ENV_FILE" \
    --env "STAGE2_PUBLIC_URL=$PUBLIC_URL" \
    --env "BRIDGE_URL=http://$NAS_IP:8765" \
    --env 'STAGE2_DB=/data/stage2.sqlite3' \
    --mount "type=bind,source=$DATA_DIR,target=/data" \
    homelab/muse-music:oauth >/dev/null
fi
docker inspect "$TAILSCALE_CONTAINER" >/dev/null 2>&1 || {
  echo "Tailscale app container was not found. No changes made." >&2
  exit 1
}
docker exec "$CONTAINER" test -f /app/server.py || {
  echo 'Existing container has no /app/server.py. No changes made.' >&2
  exit 1
}
python3 - "$CONTAINER" <<'PY'
import json, subprocess, sys
name=sys.argv[1]
data=json.loads(subprocess.check_output(['docker','inspect',name], text=True))[0]
if data.get('HostConfig',{}).get('ReadonlyRootfs'):
    raise SystemExit('Container root filesystem is read-only; refusing in-place update.')
mounts=data.get('Mounts',[])
if not any(m.get('Destination')=='/data' for m in mounts):
    raise SystemExit('Existing Muse /data mount missing; refusing to touch state.')
if not any(x.startswith('STAGE2_ENCRYPTION_KEY=') and x.split('=',1)[1]
           for x in data.get('Config',{}).get('Env',[])):
    raise SystemExit('Existing Muse encryption key missing; refusing update.')
ports=data.get('NetworkSettings',{}).get('Ports',{}).get('8788/tcp') or []
if not any(str(p.get('HostPort'))=='8788' and p.get('HostIp') in ('192.168.1.152','0.0.0.0','')
           for p in ports):
    raise SystemExit('Muse is not published at NAS port 8788; no changes made.')
print('Existing Muse container, encryption key, /data mount, and port verified.')
PY

curl --fail --silent --show-error --max-time 6 "http://$NAS_IP:8765/health" >/dev/null || {
  echo 'Music Profiles Bridge on port 8765 is unavailable. No changes made.' >&2
  exit 1
}

umask 077
TEMP="$(mktemp -d /tmp/muse-connector-patch.XXXXXXXX)"
MODIFIED=0
ORIGIN_PRESENT=0

restore_on_error() {
  code="$?"
  trap - ERR
  if [ "$MODIFIED" = 1 ]; then
    echo 'Update failed. Restoring previous Muse connector code and public origin.' >&2
    docker cp "$TEMP/server.original.py" "$CONTAINER:/app/server.py" || true
    if [ "$ORIGIN_PRESENT" = 1 ]; then
      docker cp "$TEMP/origin.original" "$CONTAINER:/data/muse-public-origin" || true
    else
      docker exec -u 0 "$CONTAINER" rm -f /data/muse-public-origin || true
    fi
    docker restart "$CONTAINER" >/dev/null || true
  fi
  exit "$code"
}
cleanup() { rm -rf -- "$TEMP"; }
trap restore_on_error ERR
trap cleanup EXIT

docker cp "$CONTAINER:/app/server.py" "$TEMP/server.original.py"
if docker exec "$CONTAINER" test -f /data/muse-public-origin; then
  ORIGIN_PRESENT=1
  docker cp "$CONTAINER:/data/muse-public-origin" "$TEMP/origin.original"
fi

RAW="https://raw.githubusercontent.com/dcazih/homelab/$MUSE_REF/muse-music-connector/server.py"
curl --fail --silent --show-error --location --retry 3 "$RAW" -o "$TEMP/server.py"
python3 -m py_compile "$TEMP/server.py"
grep -q "MUSE_CLIENT_ID = 'muse'" "$TEMP/server.py"
grep -q "PUBLIC_ORIGIN_OVERRIDE = Path('/data/muse-public-origin')" "$TEMP/server.py"

# The existing container may carry an old OAuth issuer in its environment.
# /data is a persistent volume and takes priority over the old environment.
printf '%s\n' "$PUBLIC_URL" > "$TEMP/muse-public-origin"
chmod 0644 "$TEMP/muse-public-origin" "$TEMP/server.py"

MODIFIED=1
docker cp "$TEMP/muse-public-origin" "$CONTAINER:/data/muse-public-origin"
docker cp "$TEMP/server.py" "$CONTAINER:/app/server.py"
docker restart "$CONTAINER" >/dev/null

LOCAL="http://$NAS_IP:8788"
READY=0
for n in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent --show-error --max-time 3 "$LOCAL/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" != 1 ]; then
  echo 'Updated Muse did not become healthy.' >&2
  docker logs --tail 35 "$CONTAINER" >&2 || true
  false
fi

# Check OAuth discovery, authorization page, and unauthorized MCP challenge.
curl --fail --silent --show-error --max-time 6 \
  "$LOCAL/.well-known/oauth-authorization-server" | python3 -c '
import json,sys
data=json.load(sys.stdin)
assert data["issuer"]=="https://homelab.hippocampus-polaris.ts.net:8443"
assert data["token_endpoint_auth_methods_supported"]==["none"]
assert "S256" in data["code_challenge_methods_supported"]
assert set(data["scopes_supported"])=={"music:read","music:write"}
'
CODE="$(curl --silent --show-error --max-time 6 --get \
  --output "$TEMP/auth.html" --write-out '%{http_code}' \
  --data-urlencode 'client_id=muse' \
  --data-urlencode 'redirect_uri=https://agent.meta.ai/api/hatch/oauth/callback' \
  --data-urlencode 'response_type=code' \
  --data-urlencode 'code_challenge_method=S256' \
  --data-urlencode 'code_challenge=0123456789abcdefghijklmnopqrstuvwxyzABCDEFG' \
  --data-urlencode 'scope=music:read music:write' \
  --data-urlencode 'state=connector-preflight' \
  "$LOCAL/authorize")"
if [ "$CODE" != 200 ] || ! grep -q 'Authorize Muse' "$TEMP/auth.html"; then
  echo "Muse music authorization preflight failed (HTTP $CODE)." >&2
  false
fi
CODE="$(curl --silent --show-error --max-time 6 --output /dev/null --write-out '%{http_code}' "$LOCAL/mcp")"
if [ "$CODE" != 401 ]; then
  echo "Muse MCP endpoint must challenge with 401, got HTTP $CODE." >&2
  false
fi
MODIFIED=0
echo 'Music OAuth and MCP preflight passed. Existing data and settings preserved.'

# Also update the local source tree if present, so the corrected connector
# survives a future Compose rebuild. The running container does not depend on
# the source tree being present. Keep a timestamped backup in that folder.
HOST_SOURCE='/mnt/Pool/music-server/source/muse-music-connector/server.py'
if [ -f "$HOST_SOURCE" ]; then
  HOST_BACKUP="$HOST_SOURCE.before-muse-fix.$(date +%Y%m%d%H%M%S)"
  if cp -p "$HOST_SOURCE" "$HOST_BACKUP" &&
     install -m 0644 "$TEMP/server.py" "$HOST_SOURCE"; then
    echo "Persistent source patched; backup: $HOST_BACKUP"
  else
    echo 'WARNING: running container is updated, but host source persistence failed.' >&2
  fi
else
  echo 'WARNING: no host source file found; a future container recreation could revert this patch.' >&2
fi

# This is a separate Funnel listener on :8443, not the existing root-admin
# listener on :443. Do not alter the NAS gateway or tailnet-only services.
docker exec "$TAILSCALE_CONTAINER" tailscale funnel --bg --https=8443 "$LOCAL"
echo 'Checking public music OAuth metadata...'
PUBLIC_READY=0
for n in 1 2 3 4 5; do
  if curl --fail --silent --show-error --max-time 8 \
       "$PUBLIC_URL/.well-known/oauth-authorization-server" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["issuer"]=="https://homelab.hippocampus-polaris.ts.net:8443"
assert set(d.get("scopes_supported",[]))=={"music:read","music:write"}
' >/dev/null 2>&1; then
    PUBLIC_READY=1
    break
  fi
  sleep 2
done
echo
echo "Muse Music MCP address: $PUBLIC_URL/mcp"
echo 'Muse client ID: muse'
echo 'Client secret: none'
echo 'Permissions: music:read music:write (NOT nas:root)'
echo 'Authorize with your enrolled Navidrome username and password.'
if [ "$PUBLIC_READY" = 1 ]; then
  echo 'Public music OAuth metadata verified.'
else
  echo 'WARNING: public Funnel metadata could not be verified from this NAS.' >&2
  echo "Verify $PUBLIC_URL/.well-known/oauth-authorization-server before retrying Muse." >&2
fi
echo 'Your NAS administrator gateway was not changed.'
