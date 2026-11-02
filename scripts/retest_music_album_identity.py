#!/usr/bin/env python3
"""Read-only replay of the 37 unresolved albums in 2026-11-02 report.

No tokens, users, passwords, NAS endpoints, or request POSTs are used. The
replay checks MusicBrainz identity and whether a compatible official edition
has a verifiable track count; it does not download anything.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profiles'))
from album_metadata_resolver import resolve_album, credited_as

CASES = """MIKE|Pompeii
Gue|FASTLIFE 5: Audio Luxury
Tha God Fahim|Supreme Dump Legend: Soul Cook Saga
Fiend|Iron Chef
Cookin Soul|DOOM XMAS
Cookin Soul|RAW TAPES Vol. 2
Cookin Soul|The Experience Album
Westside Gunn|Heels Have Eyes 3
Conway the Machine|I Heard You Paint Houses
Benny the Butcher|The Plugs I Met 2.5
Boldy James|Strobelight
Westside Gunn|Flygod
Westside Gunn|Hitler Wears Hermes 8: Side B
Conway the Machine|Reject 2
Benny the Butcher|The Plugs I Met
2Pac|Thug Life, Volume I
2Pac|The Don Killuminati: The 7 Day Theory
The Notorious B.I.G.|Born Again
Big L|139 & Lenox
Big L|Return of the Devil's Son
Jay-Z|Reasonable Doubt
Jay-Z|In My Lifetime, Vol. 1
Jay-Z|Vol. 2... Hard Knock Life
Jay-Z|The Blueprint
Jay-Z|The Black Album
Earl Sweatshirt|Some Rap Songs
Earl Sweatshirt|I Don't Like Shit, I Don't Go Outside
Freddie Gibbs|Pinata
MF DOOM|Madvillainy
MF DOOM|Vaudeville Villain
MF DOOM|Born Like This
Run the Jewels|Run the Jewels 4
Jay Worthy|LNDN DRGS
Jay Worthy|No Sleep for the Wicked
Jay Worthy|G-Worthy
Jay Worthy|Till the Morning
J. Cole|Might Delete Later""".splitlines()

STATUS_CHECK_FIRST = {
    ('Westside Gunn', 'Flygod'),
    ('Earl Sweatshirt', 'Some Rap Songs'),
}


class MusicBrainzReadOnly:
    def __init__(self, cache_path, interval=1.3):
        self.cache_path = cache_path
        self.cache = {}
        self.requests = 0
        self.cache_hits = 0
        self.next_time = 0.0
        self.interval = interval
        if os.path.isfile(cache_path):
            with open(cache_path,encoding='utf-8') as f:
                self.cache = json.load(f)

    async def __call__(self, path, params=None):
        params={**(params or {}),'fmt':'json'}
        url='https://musicbrainz.org/ws/2/'+path+'?'+urllib.parse.urlencode(params)
        if url in self.cache:
            self.cache_hits += 1
            return self.cache[url]
        for attempt in range(4):
            delay=max(0,self.next_time-time.monotonic())
            if delay:
                await asyncio.sleep(delay)
            self.next_time=time.monotonic()+self.interval
            req=urllib.request.Request(url,headers={
                'User-Agent':'FamilyMusicBridge/2.3 (metadata-only request replay)',
                'Accept':'application/json',
            },method='GET')
            try:
                with urllib.request.urlopen(req,timeout=23) as response:
                    data=json.load(response)
                self.requests += 1
                self.cache[url]=data
                # Persist successful reads so replay can be resumed.
                with open(self.cache_path+'.tmp','w',encoding='utf-8') as f:
                    json.dump(self.cache,f,ensure_ascii=False)
                os.replace(self.cache_path+'.tmp',self.cache_path)
                return data
            except urllib.error.HTTPError as e:
                if e.code in (429,500,502,503,504) and attempt<3:
                    try:retry_after=min(40,float(e.headers.get('Retry-After') or 0))
                    except (ValueError,TypeError):retry_after=0
                    await asyncio.sleep(max(retry_after,2**(attempt+1)))
                    continue
                raise
            except (TimeoutError,urllib.error.URLError,ConnectionError):
                if attempt>=3: raise
                await asyncio.sleep(2**(attempt+1))


async def official_edition_check(mb, gid):
    releases=await mb('release',{'release-group':gid,'limit':100})
    official=[r for r in releases.get('releases') or []
              if r.get('status')=='Official' and r.get('id')]
    official.sort(key=lambda r: (str(r.get('date') or '9999'),str(r.get('id'))))
    for release in official[:4]:
        detail=await mb('release/'+release['id'],{'inc':'recordings'})
        media=detail.get('media') or []
        count=sum(int(m.get('track-count') or len(m.get('tracks') or []))
                  for m in media if isinstance(m,dict))
        if 1<=count<=1000:
            return {'ok':True,'release_id':release['id'],'tracks':count}
    return {'ok':False,'release_id':None,'tracks':0,
            'reason':'no official edition with verifiable track count'}


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='/tmp/music_album_replay_37.json')
    parser.add_argument('--cache',default='/tmp/musicbrainz_replay_cache.json')
    parser.add_argument('--interval',type=float,default=1.3)
    args=parser.parse_args()
    if len(CASES)!=37:
        raise SystemExit('Test fixture should contain exactly 37 albums')
    mb=MusicBrainzReadOnly(args.cache,interval=args.interval)
    outcomes=[]
    for n,entry in enumerate(CASES,1):
        artist,title=entry.split('|',1)
        row={'artist':artist,'album':title,'submission':'NOT_SENT',
             'prior_status_check_required':(artist,title) in STATUS_CHECK_FIRST}
        try:
            result=await resolve_album(artist,title,mb)
            row.update(status=result['status'],match_type=result.get('match_type') or '',
                       reason=result.get('reason') or '',
                       candidates=[{'id':g.get('id'),'title':g.get('title'),
                                    'artist':credited_as(g)}
                                   for g in result.get('candidates') or []])
            if result['status']=='matched':
                group=result['group']
                row.update(release_group_mbid=group['id'],
                           canonical_artist=credited_as(group),
                           canonical_album=group['title'])
                edition=await official_edition_check(mb,group['id'])
                row.update(edition)
                row['metadata_preflight_pass']=edition['ok']
            else:
                row['metadata_preflight_pass']=False
        except Exception as exc:
            row.update(status='error',error=type(exc).__name__+': '+str(exc),
                       metadata_preflight_pass=False)
        outcomes.append(row)
        print(f"{n:02d}/37 {artist} — {title}: {row['status']}"
              f"{' / preflight OK' if row.get('metadata_preflight_pass') else ''}"
              f"{' / '+row['match_type'] if row.get('match_type') else ''}",flush=True)
        with open(args.output+'.tmp','w',encoding='utf-8') as f:
            json.dump(outcomes,f,ensure_ascii=False,indent=2)
        os.replace(args.output+'.tmp',args.output)
    summary={'attempted':len(outcomes),
             'metadata_preflight_passed':sum(bool(x.get('metadata_preflight_pass')) for x in outcomes),
             'matched_no_official':sum(x.get('status')=='matched' and not x.get('metadata_preflight_pass') for x in outcomes),
             'ambiguous':sum(x['status']=='ambiguous' for x in outcomes),
             'not_found':sum(x['status']=='not_found' for x in outcomes),
             'errors':sum(x['status']=='error' for x in outcomes),
             'prior_status_check_first':sum(bool(x['prior_status_check_required']) for x in outcomes),
             'http_requests':mb.requests,'cache_hits':mb.cache_hits}
    print('SUMMARY '+json.dumps(summary,ensure_ascii=False),flush=True)
    with open(args.output+'.summary.json','w',encoding='utf-8') as f:
        json.dump(summary,f,ensure_ascii=False,indent=2)


if __name__=='__main__':
    asyncio.run(main())
