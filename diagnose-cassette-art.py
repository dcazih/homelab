#!/usr/bin/env python3
"""Local Cassette OpenSubsonic search/artwork smoke test. Never logs auth secrets."""
from getpass import getpass
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import time

URL='http://127.0.0.1:8787/rest/'
print('Local Cassette search & artwork diagnostic. No credentials or URLs printed.')
user=input('Navidrome username: ').strip()
password=getpass('Navidrome password: ')
query=input('Search (a song you know is outside Navidrome): ').strip()
if not user or not password or len(query)<2:
    raise SystemExit('Username, password and query (2+ characters) required')

def request(endpoint, **params):
    q=urlencode({'u':user,'p':password,'f':'json','v':'1.16.1','c':'cassette-diagnostic',**params})
    req=Request(URL+endpoint+'.view?'+q,headers={'User-Agent':'CassetteDiagnostic/1.0'})
    try:
        with urlopen(req,timeout=12) as resp:
            blob=resp.read(500000)
            return resp.status,dict(resp.headers),blob
    except (HTTPError,URLError) as exc:
        print('Request error:',type(exc).__name__,getattr(exc,'code','network'))
        return 0,{},b''

def search():
    status,headers,data=request('search3',query=query,songCount='50',artistCount='10',albumCount='10')
    if not data:return None
    try:
        doc=json.loads(data)['subsonic-response']
        results=doc.get('searchResult3') or {}
        songs=results.get('song') or []
        print('HTTP:',status,'phase:',headers.get('X-Cassette-Search-Phase','unknown'),
            'songs:',len(songs),'artists:',len(results.get('artist') or []),
            'albums:',len(results.get('album') or []))
        return songs
    except (ValueError,KeyError,TypeError):
        print('Invalid OpenSubsonic JSON')
        return None

search()
print('Checking for background enrichment; the app itself will not auto-refresh...')
for _ in range(12):
    time.sleep(.3)
    status,headers,data=request('search3',query=query,songCount='50',artistCount='10',albumCount='10')
    if headers.get('X-Cassette-Search-Phase')=='enriched':break
songs=search()
if not songs:
    print('No songs in this result. Try a different, specific track title.')
    raise SystemExit()
with_art=[s for s in songs if s.get('coverArt')]
print('Songs with coverArt IDs:',len(with_art),'/',len(songs))
if not with_art:
    print('No cover IDs. The search adapter has no MusicBrainz release artwork for these recordings.')
    raise SystemExit()
for song in with_art[:3]:
    print('Checking cover for:',(song.get('title') or '')[:65])
    status,headers,data=request('getCoverArt',id=song['coverArt'],size='250')
    print('Cover HTTP:',status,'content-type:',headers.get('Content-Type','(missing)'),
          'bytes:',len(data),'looks-like-image:',data.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n')))
print('Do not share raw request URLs or your Navidrome password.')
