#!/usr/bin/env bash
set -euo pipefail
backup="${1:?Pass the backup directory printed by install.sh}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
test -f "$backup/droppedneedle-image-id.txt"
test -f "$backup/bridge-image-id.txt"
for file in "$backup"/source/droppedneedle-recovery-v1/*; do if test -f "$file"; then cp -p "$file" droppedneedle-recovery-v1/; fi; done
for file in "$backup"/source/profiles/*; do if test -f "$file"; then cp -p "$file" profiles/; fi; done
if test -d "$backup/source/profiles/assets"; then cp -Rp "$backup/source/profiles/assets/." profiles/assets/; fi
if test -d "$backup/source/droppedneedle-recovery-v1/assets"; then cp -Rp "$backup/source/droppedneedle-recovery-v1/assets/." droppedneedle-recovery-v1/assets/; fi
if ! test -f "$backup/source/droppedneedle-recovery-v1/typography.css"; then rm -f droppedneedle-recovery-v1/typography.css; fi
for name in peer_scheduler.py peer_scheduler_api.py shared_acquisition.py; do
  if ! test -f "$backup/source/profiles/$name"; then rm -f "profiles/$name"; fi
done
if ! test -f "$backup/source/droppedneedle-recovery-v1/peer_scheduler.py"; then
  rm -f droppedneedle-recovery-v1/peer_scheduler.py
fi
if ! test -f "$backup/source/droppedneedle-recovery-v1/strategy.py"; then
  rm -f droppedneedle-recovery-v1/strategy.py
fi
if test -f "$backup/source/DESIGN-TYPOGRAPHY.md"; then
  cp -p "$backup/source/DESIGN-TYPOGRAPHY.md" .
else
  rm -f DESIGN-TYPOGRAPHY.md
fi
if test -f "$backup/source/compose.peer-scheduler.yml"; then
  cp -p "$backup/source/compose.peer-scheduler.yml" .
else
  rm -f compose.peer-scheduler.yml
fi
docker tag "$(cat "$backup/droppedneedle-image-id.txt")" cassette-dn-recovery-v1:c3dd5cd127b3
docker tag "$(cat "$backup/bridge-image-id.txt")" music-profiles-bridge:2.0-local
compose=(docker compose -p music-requests-mac -f compose.yml -f compose.profiles.yml -f compose.cassette.events.yml -f droppedneedle-small-flac/compose.compact-flac.yml -f droppedneedle-recovery-v1/compose.recovery.yml)
"${compose[@]}" up -d --no-deps --no-build --force-recreate droppedneedle music-profiles
echo 'Previous images and source restored. Scheduler volume and all databases were retained.'
