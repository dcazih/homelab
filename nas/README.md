# NAS music stack

TrueNAS host paths use `/mnt/Pool/shared/music-server`. `compose.yml` is designed for TrueNAS Apps > Discover > Install via YAML; the application definition can reference the checked-out file with `include:`. The checkout at `music-server/homelab` keeps the existing Git origin.

Do not deploy this stack until all state directories and the four restricted `.env` files exist. The Mac remains the active server during initial copy. Copy a final snapshot while Mac services are stopped, then start the NAS stack and validate Navidrome library/accounts, slskd queue, DroppedNeedle requests, Bridge profiles, Sona login, and Muse before changing Tailscale routes. Keep Mac data and images until after acceptance.

`homelab-navidrome` is a separate TrueNAS custom app using the same 0.64.2 version as the Mac. The remaining services are in `compose.yml`; they reach Navidrome at `192.168.1.152:4533`. All data is stored in bind mounts, not application-managed volumes. NAS account `spammz` owns the files with UID/GID 3000.

## Tailscale cutover

The existing tailnet services `discover`, `music`, `profile`, and `profiles` currently use the Mac as their host. `tailscale-services.json` maps those same service names to the NAS LAN listeners. Authenticate the NAS Tailscale client with the existing `tag:music-server` identity, then apply the file with `tailscale serve set-config --all tailscale-services.json`. Advertise each service and approve the NAS host in the Tailscale admin console only after all four NAS endpoints pass their health checks. Drain the Mac service host after verifying the public tailnet URLs. Keep the Mac configuration available for rollback.
