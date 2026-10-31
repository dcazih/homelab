#!/usr/bin/env bash
set -euo pipefail
snapshot_root=${1:?pass a private local destination directory}
mkdir -p "$snapshot_root"
chmod 700 "$snapshot_root"
copy_data() {
  local container=$1 source=$2 target=$3
  mkdir -p "$snapshot_root/$target"
  docker cp "$container:$source/." "$snapshot_root/$target/"
}
backup_db() {
  local container=$1 source=$2 target=$3 name=$4
  docker exec "$container" python -c 'import sqlite3,sys; sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))' "$source/$name" "/tmp/migration-$name"
  docker cp "$container:/tmp/migration-$name" "$snapshot_root/$target/$name"
  rm -f "$snapshot_root/$target/$name-wal" "$snapshot_root/$target/$name-shm"
  docker exec "$container" python -c 'import os,sys; os.remove(sys.argv[1])' "/tmp/migration-$name"
}
copy_data music-profiles /data profiles
backup_db music-profiles /data profiles profiles.db
backup_db music-profiles /data profiles peer_scheduler.sqlite
copy_data music-cassette-proxy /data cassette-proxy
backup_db music-cassette-proxy /data cassette-proxy cassette-state.sqlite
copy_data muse-music /data muse
backup_db muse-music /data muse stage2.sqlite3
copy_data cassette-event-sync /state event-sync
backup_db cassette-event-sync /state event-sync events.db
