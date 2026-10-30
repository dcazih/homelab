# Tailscale routing update

Music Profiles maps the discover and music Tailscale hostnames to their respective services. The Music Player proxy now forwards non-Subsonic web requests to Navidrome; existing Subsonic routes retain their behavior.

The running proxy uses a new image, `music-cassette-proxy:navidrome-ui-v1`; the previous `cassette-proxy:stage1` image and cassette-state volume are preserved. To apply the proxy overlay after copying the patch, use:

    docker compose -p music-requests-mac -f compose.yml -f compose.profiles.yml -f compose.cassette-proxy.yml -f compose.stage4.yml -f compose.stage4-1.yml -f compose.navidrome-ui.yml up -d --no-deps --build cassette-proxy

To roll back just the web proxy to the previous image, omit `compose.navidrome-ui.yml` and run the same command with `up -d --no-deps --no-build cassette-proxy` instead. This restores the previous proxy but the Music Player web root will no longer be forwarded.

Tests: 16 isolated Bridge tests and 2 isolated proxy tests passed. Profiles and Discovery health returned HTTP 200; Music Player redirected to `/app/` and returned HTTP 200.
