#!/usr/bin/env python3
"""Safe, read-only proxy search probe. Credentials prompted, not stored or logged."""
import getpass
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST='http://127.0.0.1:8787'
print('Cassette search diagnostic. No password or tokens will be printed.')
user=input('Navidrome username: ').strip()
password=getpass.getpass('Navidrome password: ')
query=input('Song or artist to search (try a song NOT in Navidrome): ').strip()
if not user or not password or len(query)<2:
    sys.exit('Username, password and a search of at least 2 characters are required.')
payload=urllib.parse.urlencode({'u':user,'p':password,'query':query,'f':'json',
    'v':'1.16.1','c':'CassetteSearchDiagnostic','songCount':'40','albumCount':'10',
    'artistCount':'10'}).encode()
request=urllib.request.Request(HOST+'/rest/search3.view',data=payload,
    headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},method='POST')
print('Checking proxy search ...')
start=time.monotonic()
try:
    with urllib.request.urlopen(request,timeout=15) as reply:
        headers=dict(reply.headers)
        data=json.load(reply)
        status=reply.status
except urllib.error.HTTPError as e:
    print('HTTP status:',e.code)
    sys.exit('Proxy returned an HTTP error. Inspect docker logs. Do not paste secrets.')
except urllib.error.URLError as e:
    sys.exit('Proxy not reachable: '+str(e.reason))
finally:
    password=''  # Reduce lifetime of user-provided credential in this process.
print('Elapsed:',round(time.monotonic()-start,2),'seconds; HTTP:',status)
for header in ('X-Cassette-External-Count','X-Cassette-Search-DN','X-Cassette-Search-MB'):
    print(header+':',headers.get(header,'(not present: older proxy version?)'))
root=data.get('subsonic-response',{})
print('OpenSubsonic status:',root.get('status'))
if root.get('status')!='ok':
    print('OpenSubsonic error:',(root.get('error') or {}).get('message','unknown'))
else:
    songs=(root.get('searchResult3') or {}).get('song') or []
    if isinstance(songs,dict):songs=[songs]
    ext=[s for s in songs if str(s.get('id','')).startswith('cassette-request-track_')]
    local=[s for s in songs if not str(s.get('id','')).startswith('cassette-request-track_')]
    print(f'Tracks: {len(songs)} total; {len(ext)} external; {len(local)} Navidrome')
    print('First results (names only):')
    for s in songs[:10]:
        origin='EXTERNAL' if str(s.get('id','')).startswith('cassette-request-track_') else 'LOCAL'
        print(f"  [{origin}] {s.get('artist','?')} - {s.get('title','?')}")
print('Finished. Only the summary above should be shared, never credentials or request URLs.')
