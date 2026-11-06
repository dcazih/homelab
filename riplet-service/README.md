# Riplet converter and monitoring

This directory is the backend build context. It requires Node 24, ffmpeg and yt-dlp with EJS support. The monitoring database is SQLite and stores no IP addresses or submitted URLs.

On TrueNAS, copy `service/`, `public/`, and `Dockerfile` into the existing Riplet backend directory. Merge `compose.metrics.yaml` into the current VPN compose configuration, set a long random `RIPLET_ADMIN_TOKEN` in its private environment, create the persistent `../data/metrics` directory writable by the container's `node` user, then rebuild only `youtube-converter`. Keep the existing Gluetun, Caddy and other services running. The backend needs HTTPS access through the existing Caddy route. Open `https://api.riplet.cc:28934/_admin` and sign in with username `admin` and the admin token.

The dashboard shell and metrics endpoint both require HTTP Basic authentication; Bearer token requests are also accepted for controlled checks. The database keeps 31 days of events and prunes hourly. Jobs and downloads are counted separately. Pageviews are anonymous; visits are approximated once per browser tab session. Returning visitors cannot be identified without persistent tracking, which this implementation avoids. Country data is recorded only if the upstream proxy passes `CF-IPCountry`; otherwise it reads `Unknown`. Cloudflare Web Analytics can provide its own visitor and country reports independently when enabled for `riplet.cc`.

Run `node --test test/*.test.mjs` for counting and tier gate tests. Verify deployment with a real conversion and completed download, then check the 24-hour dashboard for the corresponding events. No reputation feed is configured because this deployment has no authorized threat-intelligence API.
