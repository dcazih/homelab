# Sona

React/PWA port in a separate service. The feature and interaction reference is
[`CassetteLab/cassette`](https://github.com/CassetteLab/cassette) at the commit in
`UPSTREAM-COMMIT`. Its MPL-2.0 license and notice are included here. This web
port does not replace or patch the installed native Cassette application.

## Current implementation

- Navidrome sign-in through the existing Music Profiles Bridge, with an encrypted,
  persistent, HttpOnly web session. The browser never receives the Navidrome
  password or Profiles token in a JavaScript response.
- Personalized OpenSubsonic library through the existing Cassette proxy: home,
  discovery, artists, albums, songs, playlists, search, favorites, and detail pages.
- Audio playback, queue, seek, shuffle, repeat, next/previous, mini player, and
  Media Session controls when the browser supports them. The player uses Cassette's
  blurred artwork backdrop, animated sheet, swipe down close, and horizontal
  swipe skip behavior as the interaction reference. Structured lyrics switch
  into the artwork area with a smooth transition.
- Offline song and album downloads in per-user IndexedDB storage, with playback
  preferring a saved copy. The service worker caches the application shell.
- Music Profiles Bridge requests and event-driven request status updates.
- Responsive desktop and phone layouts with installable PWA metadata.

## Work remaining for full native feature parity

The Swift app also includes radio, editable playlist and
queue controls, crossfade and replay gain, ListenBrainz, yearly Wrapped,
recommendations, and platform-specific widgets/CarPlay integrations. These are
not implemented in the current web port. Browser platform support for media
formats, background playback, storage retention, and AirPlay varies by device.

## Run

The service is isolated in `compose.cassette-web.yml`; its only persistent
Docker volume holds the session encryption key. It connects to the existing
Bridge and Cassette proxy on the `music-request` network.

```sh
docker compose -f compose.yml -f compose.cassette-web.yml build cassette-web
docker compose -f compose.yml -f compose.cassette-web.yml up -d --no-deps cassette-web
```

Local URL: `http://127.0.0.1:8790/`. A dedicated Tailscale Serve route is needed
for phone installation over HTTPS. Do not reuse an existing music or profiles
route, because that would replace the current application at that address.

Stop only this service with:

```sh
docker compose -f compose.yml -f compose.cassette-web.yml stop cassette-web
```

The volume should be retained so existing web sessions remain valid. The
current Navidrome, DroppedNeedle, Cassette proxy, and Profiles Bridge images and
volumes are not changed by this Compose overlay.

## Verification

- `npm run build` passes.
- `/health` and `/manifest.webmanifest` return HTTP 200.
- Unauthenticated `/api/session` and `/api/sub/getArtists` return HTTP 401.
- `GET /api/sub/star` returns HTTP 405; a fake sign-in returns HTTP 401.
- The new container reaches both `music-profiles:8765` and `cassette-proxy:8787`.
- Production npm dependencies have zero reported vulnerabilities at audit time.

End-to-end playback and account-specific requests still require a family member
to sign in with their existing Navidrome credentials. No production password is
included in this repository or in tests.
