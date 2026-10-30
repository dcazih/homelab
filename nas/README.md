# NAS music stack

TrueNAS keeps the Git checkout at `/mnt/Pool/shared/music-server/homelab` and a preserved copy of the migrated data under `/mnt/Pool/shared/music-server`. Runtime state, private environment files, media, and readable build sources live on the POSIX dataset `/mnt/Pool/music-server`. `compose.yml` is designed for a TrueNAS custom app whose YAML references the checked-out file with `include:`. Copy custom build sources into `/mnt/Pool/music-server/source` with readable file modes before deploying; keep private `.env` files outside that source directory.

The NAS deployment has copied state, private environment files, and a starter media set. The Mac music containers are stopped but retained, along with their state and custom images. Keep them available for rollback. `bash nas/verify.sh 192.168.1.152` checks the six local HTTP entry points. A full authenticated playback and request test should also be performed with a personal account.

`homelab-navidrome` is a separate TrueNAS custom app using the same 0.64.2 version as the Mac. The remaining services are in `compose.yml`; they reach Navidrome at `192.168.1.152:4533`. All data is stored in bind mounts, not application-managed volumes. NAS account `spammz` owns the files with UID/GID 3000.

## Tailscale cutover

The existing tailnet services `discover`, `music`, `profile`, and `profiles`, plus the Sona web client at `sona`, are hosted by the NAS. The NAS is tagged `tag:music-server`; the Mac host is drained but retained. `tailscale-serve-runtime.json` supplies `/etc/tailscale/serveconfig.json` in the official TrueNAS Tailscale app through `TS_SERVE_CONFIG`. `tailscale-services.json` is the alternate versioned CLI format; do not pass that file to `TS_SERVE_CONFIG`.

Tailscale v1.104.4 restores the route configuration after a container restart but can lose its service advertisements. TrueNAS cron job **Restore NAS Tailscale music service advertisements** (ID 1) runs `/root/advertise-music-services.sh` every minute. This root-owned script was installed from `tailscale-advertise.sh`. It advertises the five NAS services and does not change their proxy targets. A restart test confirmed that the public routes return after this task runs. If the cron job or script is removed, reinstall the script as root and enable the job before restarting Tailscale.

Public route checks: `https://discover.hippocampus-polaris.ts.net/` returns 200, `https://music.hippocampus-polaris.ts.net/` redirects to Navidrome, `https://profile.hippocampus-polaris.ts.net/` redirects to Bridge, `https://profiles.hippocampus-polaris.ts.net/` returns 200, and `https://sona.hippocampus-polaris.ts.net/` serves Sona.

For rollback, first stop or drain the NAS Tailscale host for each of the four services, then run `tailscale serve advertise svc:discover`, `svc:music`, `svc:profile`, and `svc:profiles` on the Mac. Start the preserved Mac music containers only after verifying their original state and ports. Do not delete either NAS or Mac state or custom images during rollback.
