#!/bin/sh
# Deploy the current dcazih/Sona branch to the existing NAS Sona image.
# Application data is outside this checkout; this only replaces source/image.
set -eu

repo=/mnt/Pool/music-server/source/sona-aonsoku
branch=sona-cassette-mobile
state=/mnt/Pool/music-server/state/sona-aonsoku
lock=/var/run/update-sona.lock
log="$state/deploy.log"
image=homelab/sona-aonsoku:nas
compose=/mnt/.ix-apps/app_configs/homelab-music/versions/1.0.0/templates/rendered/docker-compose.yaml
service=sona-aonsoku
host_port=192.168.1.152:8791

mkdir -p "$state"
exec 9>"$lock"
flock -n 9 || exit 0

log_line() { printf '%s %s\n' "$(date -Iseconds)" "$*" >> "$log"; }
git_repo() { git -c safe.directory="$repo" -C "$repo" "$@"; }

log_line "checking $branch"
git_repo fetch --quiet --depth=1 origin "$branch"
remote_commit=$(git_repo rev-parse FETCH_HEAD)
deployed_commit=$(cat "$state/deployed-commit" 2>/dev/null || true)

if [ "$remote_commit" = "$deployed_commit" ]; then
    log_line "unchanged $remote_commit"
    exit 0
fi

# The clone is intentionally shallow; reset to the fetched source tip.
git_repo checkout --quiet "$branch"
git_repo reset --quiet --hard FETCH_HEAD
log_line "building $remote_commit"
if ! /usr/bin/docker build -t "$image" "$repo" >> "$log" 2>&1; then
    printf '%s\n' "$remote_commit" > "$state/failed-commit"
    log_line "build failed $remote_commit"
    exit 1
fi

log_line "recreating $service for $remote_commit"
# Recreate only Sona. A full TrueNAS app redeploy restarts Navidrome,
# DroppedNeedle, slskd, and all Bridges, which makes the NAS appear down.
# TrueNAS can leave the previous ix-* container running while the rendered
# compose project creates its replacement. Remove only containers that own
# Sona's published port so the replacement cannot fail with a port conflict.
for container in $(/usr/bin/docker ps -q --filter "publish=$host_port"); do
    name=$(/usr/bin/docker inspect --format '{{.Name}}' "$container" 2>/dev/null || true)
    log_line "releasing $host_port from ${name#/}"
    /usr/bin/docker rm -f "$container" >> "$log" 2>&1
done

if /usr/bin/docker compose -f "$compose" up -d --no-deps --force-recreate "$service" >> "$log" 2>&1; then
    printf '%s\n' "$remote_commit" > "$state/deployed-commit"
    rm -f "$state/failed-commit"
    log_line "deployed $remote_commit"
else
    printf '%s\n' "$remote_commit" > "$state/failed-commit"
    log_line "redeploy failed $remote_commit"
    exit 1
fi
