# Music request server starter, Mac mini

This is a **configuration starter**, not a deployed or end-to-end tested instance.
It runs **DroppedNeedle** as the user request portal, **slskd** through **Gluetun + AirVPN**, and keeps your existing *native Mac Navidrome* serving `~/Music/Navidrome`.

## Data flow

Users (Tailscale) -> DroppedNeedle (HTTP 8688) -> slskd API (through gluetun HTTP 5030) -> AirVPN -> downloads -> DroppedNeedle verifies, tags and moves into Navidrome's music directory.

The request portal and Navidrome are **not** routed through AirVPN. Only slskd is. Tailscale is separate. Do not publicly forward the request-portal or API ports.

## Prerequisites

1. Docker Desktop running on the Mac. It uses a Linux VM to support the Gluetun container.
2. Your *existing* `~/Music/Navidrome` directory and native Navidrome server.
3. AirVPN account with port **32740** reserved, plus WireGuard credentials from the **AirVPN Config Generator** for the **same device** as that forwarded port.
4. Tailscale running on the Mac for private request-page access from your phone or other authorized users.
5. A Soulseek account. Soulseek expects sharing; supply only files you are permitted to share in the designated share directory.

## Prepare files

Run from this folder after you unzip it onto the Mac:

```bash
cp .env.example .env
mkdir -p slskd droppedneedle/config droppedneedle/cache
mkdir -p "$HOME/Music/Navidrome" "$HOME/Music/_automation/slskd-downloads" "$HOME/Music/_automation/share"
cp slskd.yml.example slskd/slskd.yml
chmod 600 .env slskd/slskd.yml
```

Edit `.env`: fill actual `HOST_UID` (`id -u`), `HOST_GID` (`id -g`), `HOST_MUSIC_ROOT` (`echo "$HOME/Music"`), AirVPN WireGuard private key, preshared key, and tunnel IPv4 address. The `.env` file is private. Don't paste the secrets into chat or commit them to GitHub.

Edit `slskd/slskd.yml`: fill Soulseek username/password, a strong web UI password, and an API key. Use `openssl rand -hex 32` to make a key. Your **existing native Mac slskd config** is under `~/Library/Application Support/slskd/slskd.yml`, but don't blindly copy it, because downloads and shares inside Docker require `/media/...` paths.

**Stop native slskd before proceeding:** it otherwise occupies local HTTP port 5030 and conflicts with the Docker version. Stop it from the Terminal running it or use `pkill -x slskd` after confirming you want to stop your native instance.

## Launch

```bash
docker compose -f compose.yml config -q
docker compose -f compose.yml up -d
docker compose -f compose.yml ps
docker compose -f compose.yml logs -f gluetun slskd droppedneedle
```

If Gluetun fails on Docker Desktop, inspect the Gluetun logs and verify Docker virtualization, WireGuard credentials, `/dev/net/tun`, and AirVPN device selection before making any requests.

### Configure DroppedNeedle

Open **http://localhost:8688** on the Mac; create your admin account.

- Settings > Library: add `/media/Navidrome`.
- Settings > Download Client: slskd URL = `http://gluetun:5030`, API key = the `web.authentication.api_keys.droppedneedle.key` you placed in `slskd.yml`.
- Use **Test** and verify the downloads mount is reported as present, writable, and on the same filesystem.
- Configure quality thresholds. For identifying the correct release, consider an AcoustID key and fingerprint verification. Enable required human approvals for normal users.
- Add **only** users you trust, and set request quotas/storage caps.

The slskd web UI is **http://localhost:5030** on the Mac, and uses its own web credentials. DroppedNeedle uses the API key.

The existing Navidrome stays at **http://localhost:4533** on the Mac. Because the importer writes into the existing host music folder, Navidrome notices new accepted files automatically. Linking Navidrome as a DroppedNeedle playback source is optional and may require making the native Navidrome service reachable from Docker (a service bound to host `127.0.0.1` won't be accessible from the container via `host.docker.internal`).

### Private access over Tailscale

Once DroppedNeedle works locally, install/connect Tailscale on each authorized device. On the Mac, with the Tailscale CLI available:

```bash
tailscale serve --bg 8688
tailscale serve status
```

Tailscale displays your private HTTPS URL. It is accessible to authorized tailnet devices, **not** the public Internet. Keep per-user DroppedNeedle logins/permissions enabled. Do not use Tailscale Funnel or public router port forwarding for this request UI.

### AirVPN port forwarding

Port **32740/TCP** was verified open in your native slskd setup. The Docker connection uses a separately generated WireGuard configuration. **Do not assume the old AirVPN port result carries over.** Make sure the forwarding rule applies to the device/key used in `.env`. Verify `listening on 0.0.0.0:32740` in slskd logs, then run the AirVPN port test while Gluetun is connected.

There is no `32740` host port mapping in Compose. That is intentional: inbound peers connect to AirVPN and its VPN-side forwarded port, not to the Mac's public interface.

## What the native checks do

DroppedNeedle documents search preflight scoring, tag readability, duration tolerance, optional AcoustID fingerprint/release matching, quarantine of failed candidates, and held-import manual review. Bad peers/files are excluded from later rankings; partial or failed requests go to a background wanted watcher.

**Not included in this starter**: antivirus scanning; guaranteed immediate failover to the *next* candidate after a failed import; automatic user push alerts on each failed candidate. These need a custom DroppedNeedle validation/orchestrator extension. Do not claim they are enabled. The request itself does show status/partial/failed and manual review in the UI.

Recommended custom extension when you're ready:

1. Download only into quarantine/staging, never directly into `/media/Navidrome`.
2. Validate path boundaries, real file type, allowlisted extensions, ffprobe decode, duration, title/release fingerprint, file-size bounds and a malware scan.
3. Only if **all required checks pass**, tag/cover-art and atomically promote to Navidrome.
4. Otherwise record explicit failure and safely quarantine; immediately try the next candidate with a finite retry cap. Show users a status, and notify them if the request is exhausted or manual review is required.

A malware scanner is defense in depth, not proof a downloaded file is safe. Never execute files from download staging. Only import files you have rights to obtain and share.

## Key docs

- https://droppedneedle.com/docs/getting-started
- https://droppedneedle.com/docs/native-engine
- https://droppedneedle.com/docs/slskd-setup
- https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/airvpn.md
- https://github.com/qdm12/gluetun-wiki/blob/main/setup/inter-containers-networking.md
- https://tailscale.com/docs/reference/tailscale-cli/serve
