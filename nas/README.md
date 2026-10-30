# NAS music stack

TrueNAS keeps the Git checkout at `/mnt/Pool/shared/music-server/homelab` and a preserved copy of the migrated data under `/mnt/Pool/shared/music-server`. Runtime state, private environment files, media, and readable build sources live on the POSIX dataset `/mnt/Pool/music-server`. `compose.yml` is designed for a TrueNAS custom app whose YAML references the checked-out file with `include:`. Copy custom build sources into `/mnt/Pool/music-server/source` with readable file modes before deploying; keep private `.env` files outside that source directory.

Do not deploy this stack until all state directories and the four restricted `.env` files exist. The Mac remains the active server during initial copy. Copy a final snapshot while Mac services are stopped, then start the NAS stack and validate Navidrome library/accounts, slskd queue, DroppedNeedle requests, Bridge profiles, Sona login, and Muse before changing Tailscale routes. Keep Mac data and images until after acceptance.

`homelab-navidrome` is a separate TrueNAS custom app using the same 0.64.2 version as the Mac. The remaining services are in `compose.yml`; they reach Navidrome at `192.168.1.152:4533`. All data is stored in bind mounts, not application-managed volumes. NAS account `spammz` owns the files with UID/GID 3000.

## Tailscale cutover

The existing tailnet services `discover`, `music`, `profile`, and `profiles` currently use the Mac as their host. `tailscale-services.json` maps those same service names to the NAS LAN listeners. Authenticate the NAS Tailscale client with the existing `tag:music-server` identity, then apply the file with `tailscale serve set-config --all tailscale-services.json`. Advertise each service and approve the NAS host in the Tailscale admin console only after all four NAS endpoints pass their health checks. Drain the Mac service host after verifying the public tailnet URLs. Keep the Mac configuration available for rollback.
