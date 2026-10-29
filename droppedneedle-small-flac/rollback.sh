#!/usr/bin/env bash
set -euo pipefail
docker compose -p music-requests-mac --project-directory /Users/squarescan/Downloads/music-requests-mac -f /Users/squarescan/Downloads/music-requests-mac/compose.yml -f /Users/squarescan/Downloads/music-requests-mac/compose.profiles.yml -f /Users/squarescan/Downloads/music-requests-mac/compose.cassette.events.yml -f /Users/squarescan/Downloads/music-requests-mac/droppedneedle-small-flac/compose.rollback.yml up -d --no-deps --no-build --force-recreate droppedneedle
echo "Original Compose image restored; data untouched."
