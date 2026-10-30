#!/bin/sh
# Deploy new commits from dcazih/Sona without overlapping builds.
set -eu

repo=/mnt/Pool/music-server/source/sona-aonsoku
branch=sona-cassette-mobile
state=/mnt/Pool/music-server/state/sona-aonsoku
lock=/var/run/update-sona.lock

mkdir -p "$state"
exec 9>"$lock"
flock -n 9 || exit 0

git_repo() {
    git -c safe.directory="$repo" -C "$repo" "$@"
}

git_repo fetch --quiet --depth=1 origin "$branch"
remote_commit=$(git_repo rev-parse FETCH_HEAD)
deployed_commit=$(cat "$state/deployed-commit" 2>/dev/null || true)

if [ "$remote_commit" = "$deployed_commit" ]; then
    exit 0
fi

git_repo checkout --quiet "$branch"
# The NAS clone is intentionally shallow. A rewritten remote branch can have
# no shared history with its old shallow tip, so deploy the exact fetched tip.
# This checkout is source-only; application state lives outside this directory.
git_repo reset --quiet --hard FETCH_HEAD

if midclt call -j app.redeploy homelab-music; then
    printf '%s\n' "$remote_commit" > "$state/deployed-commit"
else
    printf '%s\n' "$remote_commit" > "$state/failed-commit"
    exit 1
fi
