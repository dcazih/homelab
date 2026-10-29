#!/usr/bin/env bash
set -euo pipefail
docker compose -p music-requests-mac --project-directory /Users/squarescan/Downloads/music-requests-mac -f /Users/squarescan/Downloads/music-requests-mac/compose.yml -f /Users/squarescan/Downloads/music-requests-mac/compose.profiles.yml -f /Users/squarescan/Downloads/music-requests-mac/compose.cassette.events.yml -f /Users/squarescan/Downloads/music-requests-mac/droppedneedle-small-flac/compose.compact-flac.yml -f /Users/squarescan/Downloads/music-requests-mac/droppedneedle-recovery-v1/compose.rollback.yml up -d --no-deps --no-build --force-recreate droppedneedle
echo "Pre-recovery image restored with original data mounts."
