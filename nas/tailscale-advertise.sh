#!/bin/sh
# TrueNAS Tailscale v1.104.4 can lose service advertisements on container restart
# even when TS_SERVE_CONFIG restores the routes. Run this as root from TrueNAS cron.
set -eu

container=ix-tailscale-tailscale-1
for service in discover music profile profiles sona muse; do
    /usr/bin/docker exec "$container" tailscale serve advertise "svc:$service"
done
