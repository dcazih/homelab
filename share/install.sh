#!/usr/bin/env bash
set -euo pipefail
root="${1:-/Users/squarescan/Downloads/music-requests-mac}"
package="$(cd "$(dirname "$0")" && pwd)/patch"
cd "$root"
test -f compose.yml
test -f droppedneedle-recovery-v1/Dockerfile
test -f profiles/app.py
test -d share
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$root/.peer-scheduler-backups/$stamp"
mkdir -p "$backup/source/droppedneedle-recovery-v1" "$backup/source/profiles" "$backup/data"
for name in Dockerfile slskd_repository.py peer_scheduler.py strategy.py typography.css; do
  if test -f "droppedneedle-recovery-v1/$name"; then cp -p "droppedneedle-recovery-v1/$name" "$backup/source/droppedneedle-recovery-v1/$name"; fi
done
for name in Dockerfile app.py index.html peer_scheduler.py peer_scheduler_api.py shared_acquisition.py album_request_bridge_v6.py; do
  if test -f "profiles/$name"; then cp -p "profiles/$name" "$backup/source/profiles/$name"; fi
done
if test -d profiles/assets; then cp -Rp profiles/assets "$backup/source/profiles/assets"; fi
if test -d droppedneedle-recovery-v1/assets; then cp -Rp droppedneedle-recovery-v1/assets "$backup/source/droppedneedle-recovery-v1/assets"; fi
if test -f DESIGN-TYPOGRAPHY.md; then cp -p DESIGN-TYPOGRAPHY.md "$backup/source/"; fi
if test -f compose.peer-scheduler.yml; then cp -p compose.peer-scheduler.yml "$backup/source/"; fi
docker inspect music-requests --format '{{.Image}}' > "$backup/droppedneedle-image-id.txt"
docker inspect music-profiles --format '{{.Image}}' > "$backup/bridge-image-id.txt"
docker tag "$(cat "$backup/droppedneedle-image-id.txt")" "cassette-dn-recovery-v1:pre-peer-$stamp"
docker tag "$(cat "$backup/bridge-image-id.txt")" "music-profiles-bridge:pre-peer-$stamp"
docker exec music-profiles python -c 'import sqlite3; s=sqlite3.connect("file:/data/profiles.db?mode=ro",uri=True); d=sqlite3.connect("/tmp/peer-install-profiles.db"); s.backup(d); d.close(); s.close()'
docker cp music-profiles:/tmp/peer-install-profiles.db "$backup/data/profiles.db"
python3 -c 'import sqlite3,sys; s=sqlite3.connect("file:droppedneedle/cache/library.db?mode=ro",uri=True); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()' "$backup/data/droppedneedle-library.db"
for name in Dockerfile slskd_repository.py peer_scheduler.py strategy.py typography.css; do cp -p "$package/droppedneedle-recovery-v1/$name" "droppedneedle-recovery-v1/$name"; done
for name in Dockerfile app.py index.html peer_scheduler.py peer_scheduler_api.py shared_acquisition.py album_request_bridge_v6.py; do cp -p "$package/profiles/$name" "profiles/$name"; done
mkdir -p profiles/assets droppedneedle-recovery-v1/assets
cp -Rp "$package/profiles/assets/." profiles/assets/
cp -Rp "$package/droppedneedle-recovery-v1/assets/." droppedneedle-recovery-v1/assets/
cp -p "$package/DESIGN-TYPOGRAPHY.md" .
cp -p "$package/compose.peer-scheduler.yml" .
docker volume create music-requests-mac_peer-scheduler-data >/dev/null
docker run --rm --network none --user root -v music-requests-mac_peer-scheduler-data:/peer-scheduler-data --entrypoint sh python:3.12-slim -c 'chmod 777 /peer-scheduler-data; test ! -f /peer-scheduler-data/scheduler.sqlite || chmod 666 /peer-scheduler-data/scheduler.sqlite'
compose=(docker compose -p music-requests-mac -f compose.yml -f compose.profiles.yml -f compose.cassette.events.yml -f droppedneedle-small-flac/compose.compact-flac.yml -f droppedneedle-recovery-v1/compose.recovery.yml -f compose.peer-scheduler.yml)
"${compose[@]}" config -q
"${compose[@]}" build droppedneedle music-profiles
"${compose[@]}" up -d --no-deps --no-build --force-recreate droppedneedle music-profiles
printf 'Installed. Backup: %s\n' "$backup"
printf 'Rollback: %s/share/rollback.sh %s\n' "$root" "$backup"
