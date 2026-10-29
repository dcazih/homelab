"""Music-aware typo fallback and public ListenBrainz popularity rankings.

No credentials are sent to either MusicBrainz or ListenBrainz. Remote calls are
bounded/cached, not performed on every keystroke (only $-mode discovery).
"""
import asyncio
import logging
import re
import time
import unicodedata

import httpx

LOG = logging.getLogger('cassette_music_search')
STOP = {'a','an','and','of','on','at','to','for','from','the','in','by','with','feat','ft'}
LB_BASE='https://api.listenbrainz.org/1/popularity'
_POP_CACHE={}
_POP_TTL=3600


def norm(value):
    raw=unicodedata.normalize('NFKD',str(value or '').casefold())
    raw=''.join(c for c in raw if not unicodedata.combining(c))
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in raw).split())


def _osa(a,b):
    """Damerau optimal-string-alignment distance: typo transposition costs one."""
    if a==b:return 0
    prevprev=None;prev=list(range(len(b)+1))
    for i,ca in enumerate(a,1):
        cur=[i]+[0]*len(b)
        for j,cb in enumerate(b,1):
            cur[j]=min(cur[j-1]+1,prev[j]+1,prev[j-1]+int(ca!=cb))
            if prevprev is not None and i>1 and j>1 and ca==b[j-2] and a[i-2]==cb:
                cur[j]=min(cur[j],prevprev[j-2]+1)
        prevprev,prev=prev,cur
    return prev[-1]


def typo_distance(query,candidate):
    """Conservative correction: do not rewrite short words, IDs or proper names."""
    qw=norm(query).split();cw=norm(candidate).split()
    if not qw or len(qw)!=len(cw) or len(qw)>12:return None
    if sum(map(len,qw))<5:return None
    total=0
    for a,b in zip(qw,cw):
        if a==b:continue
        if min(len(a),len(b))<3:return None
        cost=_osa(a,b)
        if cost>(1 if min(len(a),len(b))<5 else 2):return None
        total+=cost
        if total>2:return None
    return total if total else None


def best_spelling(query,names):
    """RapidFuzz breaks distance ties; only validated close typo variants qualify."""
    try:
        from rapidfuzz.fuzz import ratio
    except ImportError:
        ratio=lambda a,b: 100*(1-_osa(a,b)/max(1,len(a),len(b)))
    ranked=[]
    for name in dict.fromkeys(x for x in names if isinstance(x,str)):
        dist=typo_distance(query,name)
        if dist is None:continue
        ranked.append((dist,-ratio(norm(query),norm(name)),name))
    ranked.sort()
    return [r[2] for r in ranked[:3]]


def symspell_candidate(query):
    """Optional SymSpell compound suggestion, never trusted without catalog validation."""
    if not norm(query).isascii():return None
    try:
        from symspellpy import SymSpell
        from importlib.resources import files
        # Lazy initialized: dictionary load happens once on a true miss.
        global _SYMSPELL
        if _SYMSPELL is None:
            sym=SymSpell(max_dictionary_edit_distance=2,prefix_length=7)
            path=files('symspellpy').joinpath('frequency_dictionary_en_82_765.txt')
            if not sym.load_dictionary(str(path),0,1):return None
            _SYMSPELL=sym
        options=_SYMSPELL.lookup_compound(query,max_edit_distance=2)
        if not options:return None
        suggestion=options[0].term
        return suggestion if typo_distance(query,suggestion) is not None else None
    except (ImportError,OSError,ValueError) as exc:
        LOG.debug('SymSpell fallback unavailable: %s',type(exc).__name__)
        return None


_SYMSPELL=None


def fuzzy_lucene(query,field):
    meaningful=[w for w in norm(query).split() if w not in STOP and len(w)>=4]
    if not meaningful or len(meaningful)>6:return ''
    return ' AND '.join(f'{field}:{w}~0.6' for w in meaningful[:4])


async def spelling_candidates(d,client,query):
    """Find a music-validated correction. Only called on empty external results.

    Try local known music names, optional SymSpell, then MusicBrainz fuzzy names.
    Final acceptance is a NONEMPTY actual catalog response under corrected query.
    """
    known=[]
    try:
        with d.connect() as db:
            for table,column in [('discovery_artists','name'),('discovery_albums','name'),('discovery','title')]:
                known += [row[0] for row in db.execute(
                    f'SELECT DISTINCT {column} FROM {table} ORDER BY created DESC LIMIT 900')]
    except Exception:
        pass
    candidate_names=list(known)
    # MusicBrainz fuzzy querying uses the existing MB rate gate, cache and user agent.
    for category,field,key in [('artist/','artist','artists'),
                                ('release-group/','releasegroup','release-groups'),
                                ('recording/','recording','recordings')]:
        query_string=fuzzy_lucene(query,field)
        if not query_string:continue
        try:
            result=await d._artist_mb_get(client,category,{'query':query_string,'limit':15})
            if isinstance(result,dict):
                candidate_names += [row.get('name') or row.get('title') or ''
                                    for row in result.get(key,[]) if isinstance(row,dict)]
        except Exception as exc:
            LOG.warning('Spelling metadata source unavailable: %s',type(exc).__name__)
        # If this category already gave us close music names we can avoid two more calls.
        if best_spelling(query,candidate_names):break
    results=best_spelling(query,candidate_names)
    english=symspell_candidate(query)
    if english and english not in results:results.append(english)
    return results[:3]


async def popular_for_artist(client,mbid):
    """ListenBrainz listen-count based rankings; public and token-free.

    Fail open: return None so the caller retains MusicBrainz discovery.
    """
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',str(mbid),re.I):
        return None
    hit=_POP_CACHE.get(mbid)
    if hit and time.monotonic()-hit[0]<_POP_TTL:return hit[1]

    async def fetch(kind):
        try:
            r=await client.get(f'{LB_BASE}/top-{kind}-for-artist/{mbid}',
                headers={'Accept':'application/json','User-Agent':'CassetteFamilyProxy/1.0'},
                timeout=4.0)
            r.raise_for_status()
            payload=r.json()
            return payload if isinstance(payload,list) else []
        except (httpx.HTTPError,ValueError) as exc:
            LOG.info('ListenBrainz %s unavailable: %s',kind,type(exc).__name__)
            return []

    recordings,groups=await asyncio.gather(fetch('recordings'),fetch('release-groups'))
    result={'recordings':recordings,'groups':groups}
    if len(_POP_CACHE)>200:_POP_CACHE.clear()
    _POP_CACHE[mbid]=(time.monotonic(),result)
    return result


def count(row):
    try:return max(0,int(row.get('total_listen_count') or 0))
    except (ValueError,TypeError):return 0


def top_albums(d,groups,ranking):
    """Order only VERIFIED MusicBrainz release groups by ListenBrainz listens."""
    scores={}
    for row in ranking:
        if not isinstance(row,dict):continue
        gid=str(row.get('release_group_mbid') or '').lower()
        if d.MBID.fullmatch(gid):scores[gid]=max(scores.get(gid,0),count(row))
    return sorted(groups,key=lambda x:(scores.get(str(x['musicbrainz_id']).lower(),-1),
                                      str(x.get('year') or '0000')),reverse=True)


def top_songs(d,identity,ranking,limit=15):
    """Artist-scoped, popularity-ordered recording previews, one per title.

    Top-recordings-for-artist is an artist-ID endpoint, not string matching.
    """
    seen=set();result=[]
    for row in sorted((r for r in ranking if isinstance(r,dict)),key=count,reverse=True):
        mbid=str(row.get('recording_mbid') or '')
        title=str(row.get('recording_name') or '').strip()
        if not d.MBID.fullmatch(mbid) or not title:continue
        key=d.normalized(title)
        if not key or key in seen or key in {'intro','outro','interlude','skit'}:continue
        if not count(row):continue
        seen.add(key)
        song=d.add_candidate(mbid,identity['canonical'],title,row.get('release_name') or '',
            int((row.get('length') or 0)/1000),
            release_mbid=row.get('release_mbid') or row.get('caa_release_mbid') or '')
        if song:result.append(song)
        if len(result)>=limit:break
    return result


# CASSETTE_SMART_SEARCH_V5: handle typos inside longer artist/song queries.
_spelling_candidates_v4 = spelling_candidates

async def spelling_candidates(d,client,query):
    from spelling_v5 import suggestions
    return await suggestions(d,client,query,_spelling_candidates_v4)
