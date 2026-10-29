#!/usr/bin/env bash
set -eu
PROXY="$(docker ps --format '{{.Names}}' | grep -m1 'cassette-proxy' || true)"
if [[ -z "$PROXY" ]]; then echo 'Cassette proxy container not found'; exit 1; fi
BRIDGE="$(docker ps --format '{{.Names}}' | grep -m1 '^music-profiles$' || true)"
echo '1) Checking proxy process and feature switches'
curl -fsS http://127.0.0.1:8787/health || true
printf '\n'
echo '2) Checking proxy -> Music Profiles Bridge and MusicBrainz from inside container'
docker exec -i "$PROXY" python - <<'PY'
import asyncio,os,httpx
async def main():
  async with httpx.AsyncClient(trust_env=False, follow_redirects=False,timeout=7) as client:
    tests=[('Music Bridge','http://music-profiles:8765/health'),
           ('MusicBrainz','https://musicbrainz.org/ws/2/recording/?query=Radiohead&fmt=json&dismax=true&limit=1')]
    for label,url in tests:
      try:
        headers={'User-Agent':os.environ.get('MUSICBRAINZ_USER_AGENT','CassetteSearchProbe/1.0 (example@example.com)')} if label=='MusicBrainz' else {}
        res=await client.get(url,headers=headers)
        print(label, 'HTTP',res.status_code, 'json=',res.headers.get('content-type','').startswith('application/json'))
      except Exception as e:
        print(label,'ERROR',type(e).__name__)
    print('Internal key available:', bool(os.getenv('CASSETTE_BRIDGE_INTERNAL_KEY')))
asyncio.run(main())
PY
if [[ -n "$BRIDGE" ]]; then
    echo '3) Checking Bridge -> DroppedNeedle service (auth not needed for connectivity)'
    docker exec -i "$BRIDGE" python - <<'PY'
import asyncio,os,httpx
async def main():
  url=os.getenv('DROPPEDNEEDLE_URL','http://droppedneedle:8688').rstrip('/')+'/api/v1/auth/providers'
  try:
    async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
      res=await client.get(url)
      print('DroppedNeedle HTTP',res.status_code, '(200 or 401 confirms reachable)')
  except Exception as e:
    print('DroppedNeedle ERROR',type(e).__name__)
asyncio.run(main())
PY
fi
