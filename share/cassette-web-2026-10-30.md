# Cassette Web build record — 2026-10-30

- Source: `cassette-web/` and `compose.cassette-web.yml`.
- Upstream reference: CassetteLab/cassette, commit `aed1663d21df7d1cec82d3ece438c27c83c782fc`.
- Installed image: `cassette-web:local`; container: `music-cassette-web`.
- New volume: `music-requests-mac_cassette-web-state`, holding only the web session key.
- Local URL: `http://127.0.0.1:8790/`.
- `npm run build`: passed.
- `npm audit --omit=dev`: 0 vulnerabilities reported.
- Python compile check: passed.
- HTTP: health, manifest, and 192/512 PWA icons return 200.
- Authentication boundary: unauthenticated session, streaming, and event paths return 401; disallowed Bridge admin path returns 404; mutation via GET returns 405; fake sign-in returns 401.
- Container network: existing Profiles Bridge and Cassette proxy health endpoints each returned 200 from the new container.
- Browser visual check: login page rendered at the local URL.
- A signed-in browser session displayed live albums, artwork, playlists, favorites, and Discover recommendations from the existing music services.
- Lyrics surface and settings screen were added after the first visual check; the production React build passed again.
- Existing music containers were left running. The Compose update targeted only `cassette-web` with `--no-deps`.

Tailnet publication remains pending: automatic approval review rejected the new persistent `svc:cassette` route because it would expand network access. No Tailscale configuration was changed. See `cassette-web/README.md` for current feature coverage and gaps.

Rollback: `docker compose -f compose.yml -f compose.cassette-web.yml stop cassette-web`. Retain the volume to preserve existing sessions and the encryption key.
