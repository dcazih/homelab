# Sona update — 2026-10-30

Changed the React web port at `cassette-web/` and rebuilt only `music-cassette-web`.

## Delivered
- Sona name, manifest, library navigation, Home/New/Radio/Search/Library tabs, Apple Music inspired dark layout, profile menu, search browse tiles.
- Custom pointer and keyboard playback timeline with bounded remaining time and duration fallback.
- Server backed favorites with immediate UI updates and rollback on failure.
- Up Next queue display, reorder, remove, and clear controls; volume control.
- Settings sections corresponding to Cassette: Downloads, Cache, Playback, Crossfade, Server, Integrations, About. Preferences are stored per Navidrome user in this browser.
- Nonselectable interface text except form fields.

## Verification
`npm run build` passed. `npm test` passed (2 tests). Browser verified Sona loads on `http://127.0.0.1:8790/`; player timeline showed `0:18 - 2:19` for YAH.; favorite button toggled immediately and was restored; settings appeared.

## Known limitations
Crossfade, automatic cache management, ListenBrainz submission, and AudioMuse connection are not yet wired to their respective engines. The settings for these appear and persist but do not affect playback or integrations. ReplayGain depends on track gain metadata from the server. Radio stream controls are separate from the main player. Continue Across Devices depends on the server's play queue endpoint.

## Rollback
The previous custom Docker image remains in the local image cache if not pruned. To restore earlier UI source, use the repository's local history or a saved copy of `cassette-web/`, then run `docker compose -f compose.yml -f compose.cassette-web.yml up -d --build --no-deps cassette-web`. No persistent music data or native Cassette files were changed for this update.
