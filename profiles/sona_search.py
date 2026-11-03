"""Bounded identity-first Sona search; metadata evidence is not download availability.

Uses the requester's MusicBrainz cache and exact title/credit validators. Artist
aliases and collaboration membership are read from MusicBrainz, never invented.
No acquisition or request POST is performed by search.
"""
import asyncio
import re
import time
import httpx
from collections import OrderedDict
from difflib import SequenceMatcher
import edition_matching as mb
from metadata_resolver_v1 import fold, same_recording, artist_matches

RESULTS = 5
_cache = OrderedDict()
_tasks = {}
_waiters = {}


def safe(value):
    return re.sub(r'[^\w\s\-\u2010-\u2015]', ' ', str(value), flags=re.UNICODE).strip()[:160]


def album_title(value):
    return re.sub(r'\s*(?:\((?:deluxe|expanded|remastered|anniversary)[^)]*\)|- (?:single|ep))\s*$', '',str(value or ''),flags=re.I).strip()

def paired_title(query, title):
    words=fold(query).split(); title=fold(title)
    for index in range(1,min(4,len(words))):
        suffix=' '.join(words[index:])
        if title==suffix:return ' '.join(words[:index]),110
        if len(suffix)>=5 and title.startswith(suffix+' '):return ' '.join(words[:index]),102
    return '',0


_hint_calls=[]
async def relevance_hints(query):
    # Small, cached public catalog prior; names only, never store IDs, artwork,
    # previews or acquisition availability. Every returned identity is MB-verified.
    key=mb._mb_cache_key('sona-relevance-hint',{'query':fold(query)})
    cached=mb._mb_cached(key)
    if cached and time.time()-cached.get('at',0)<600:return cached.get('rows',[])
    now=time.monotonic()
    _hint_calls[:]=[t for t in _hint_calls if now-t<60]
    if len(_hint_calls)>=18:return []
    _hint_calls.append(now)
    try:
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            response=await client.get('https://itunes.apple.com/search',params={'term':query,'media':'music','entity':'song','limit':10,'country':'US'})
            response.raise_for_status()
            rows=[{'title':r.get('trackName',''),'artist':r.get('artistName',''),'album':album_title(r.get('collectionName'))} for r in response.json().get('results',[])]
        mb._mb_save(key,{'at':time.time(),'rows':rows})
        return rows
    except (httpx.HTTPError,ValueError,TypeError):return []


def affinity(query, name):
    q, n = fold(query), fold(name)
    if not q or not n: return 0
    if q == n: return 100
    # Prefix completion must cover the full query, not just one common word.
    if len(q) >= 5 and n.startswith(q) and len(q) / len(n) >= .65: return 91
    # Same conservative policy as Cassette's verified artist typo resolver:
    # aligned words, no short-name guessing, at most two edits overall.
    qw, nw = q.split(), n.split()
    if len(q) < 6 or len(qw) != len(nw): return 0
    total = 0
    for a, b in zip(qw, nw):
        if a == b: continue
        if min(len(a),len(b)) < 3: return 0
        before = None
        prev = list(range(len(b)+1))
        for i, x in enumerate(a,1):
            cur = [i]
            for j,y in enumerate(b,1):
                cur.append(min(cur[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
                if before is not None and i>1 and j>1 and x==b[j-2] and a[i-2]==y:cur[j]=min(cur[j],before[j-2]+1)
            before,prev = prev,cur
        total += prev[-1]
    return 87 if total <= 2 and SequenceMatcher(None,q,n).ratio() >= .82 else 0


def credits(row):
    return [part['artist'] for part in row.get('artist-credit',[]) if isinstance(part,dict) and isinstance(part.get('artist'),dict)]


def credit(row):
    return ''.join(str(part.get('name') or (part.get('artist') or {}).get('name') or '')+str(part.get('joinphrase') or '') for part in row.get('artist-credit',[]) if isinstance(part,dict)).strip()


def valid(value): return bool(mb.MBID.fullmatch(str(value or '')))


def unique(rows, kind, limit=RESULTS):
    result=[]; ids=set(); names=set()
    for row in sorted(rows,key=lambda x:x.get('_rank',0),reverse=True):
        if not valid(row.get('id')) or row['id'] in ids: continue
        key=row['id'] if kind=='artist' else (fold(row.get('title')),fold(row.get('artist')))
        if key in names: continue
        ids.add(row['id']);names.add(key)
        result.append({k:v for k,v in row.items() if not k.startswith('_')})
        if len(result)>=limit:break
    return result


async def artist_candidates(query, allow_fuzzy=True):
    text=safe(query)
    data=await mb.mb_get('artist',{'query':f'artist:"{text}" OR alias:"{text}"','limit':12})
    rows=data.get('artists') or []
    candidates=[]
    for row in rows:
        if not valid(row.get('id')):continue
        names=[row.get('name')]+[a.get('name') for a in row.get('aliases',[]) if isinstance(a,dict)]
        score=max((artist_affinity(query,n) for n in names),default=0)
        if score:candidates.append((score,row))
    if not candidates:
        # Alias arrays can be omitted from search hits. Verify the actual entity
        # before accepting a high-scoring alias hit or attempting fuzzy expansion.
        for row in rows[:2]:
            if valid(row.get('id')) and int(row.get('score') or 0)>=95:
                details=await mb.mb_get('artist/'+row['id'],{'inc':'aliases+artist-rels'})
                names=[details.get('name','')]+[a.get('name','') for a in details.get('aliases',[]) if isinstance(a,dict)]
                score=max((artist_affinity(query,n) for n in names),default=0)
                if score:candidates.append((score,{**row,'aliases':details.get('aliases',[])}))
    if allow_fuzzy and not candidates and len(fold(query))>=6:
        tokens=safe(query).split()[:4]
        fuzzy=' OR '.join(' AND '.join(f'{field}:{token}~0.65' for token in tokens) for field in ('artist','alias'))
        data=await mb.mb_get('artist',{'query':fuzzy,'limit':8})
        for row in data.get('artists',[]):
            if not valid(row.get('id')):continue
            score=max([artist_affinity(query,row.get('name'))]+[artist_affinity(query,a.get('name')) for a in row.get('aliases',[]) if isinstance(a,dict)])
            if score:candidates.append((score,row))
    if allow_fuzzy and not candidates and len(text.split())>1:
        words=text.split()
        prefix=' AND '.join('artist:'+w+('*' if i==len(words)-1 else '') for i,w in enumerate(words))
        data=await mb.mb_get('artist',{'query':prefix,'limit':6})
        for row in data.get('artists',[]):
            score=artist_affinity(query,row.get('name'))
            if score>=91 and valid(row.get('id')):candidates.append((score,row))
    candidates.sort(key=lambda x:(x[0],int(x[1].get('score') or 0)),reverse=True)
    # Verify aliases on real artist entities; search engine alias hits alone do
    # not establish identity. Bounded to two convincing candidates.
    verified=[]
    for score,row in candidates[:2]:
        details=await mb.mb_get('artist/'+row['id'],{'inc':'aliases+artist-rels'})
        aliases=[a.get('name','') for a in details.get('aliases',[]) if isinstance(a,dict)]
        names=[details.get('name','')]+aliases
        evidence=max((artist_affinity(query,n) for n in names),default=0)
        if not evidence:continue
        verified.append({**details,'_rank':evidence,'_provider_score':int(row.get('score') or 0),'_names':names})
    return verified


def group_item(row, rank):
    gid=row.get('id')
    return {'id':gid,'kind':'album','title':row.get('title',''),'artist':credit(row),
            'year':str(row.get('first-release-date') or '')[:4],
            'releaseType':row.get('primary-type') or 'Album',
            'cover':'https://coverartarchive.org/release-group/'+str(gid)+'/front-500',
            'requestable':False,'_rank':rank + (10 if row.get('primary-type') == 'Album' else 0),'_raw':row}


_VERSION_WORDS = re.compile(r'\b(instrumental|karaoke|tribute|cover version|live|remix|rehearsal)\b', re.I)

def artist_affinity(query, name):
    score = affinity(query, name)
    q, n = fold(query), fold(name)
    # Token-prefix completion is valid evidence for artists, not a generic
    # shared-word match for songs/albums. Popularity corroborates the intent.
    if len(q) >= 4 and n.startswith(q + ' '):
        return max(score, 93)
    return score


def hint_intent(query, hints):
    """Compare independent title, credit and artist+album evidence together."""
    artists = {}
    for hint in hints:
        for name in re.split(r'\s*(?:,| & | feat\. )\s*', hint['artist']):
            score = artist_affinity(query, name)
            if score:
                entry = artists.setdefault(name, [score, set()])
                entry[1].add(fold(hint['title']))
    if artists:
        name, (score, titles) = max(artists.items(), key=lambda x:(len(x[1][1]),x[1][0]))
        if score >= 87 and (len(titles) >= 2 or affinity(query,name) >= 100):
            return 'artist', name, None
    combined = [h for h in hints if fold(query)==fold(h['artist']+' '+h['title'])]
    if combined:return 'song', combined[0]['title'], combined[0]
    songs = [h for h in hints if affinity(query,h['title']) >= 91]
    pairs = [h for h in hints if paired_title(query,h['album'])[1] and any(affinity(paired_title(query,h['album'])[0],n)>=87 for n in re.split(r'\s*(?:,| & )\s*',h['artist']))]
    if pairs:
        best = max(pairs,key=lambda h:paired_title(query,h['album'])[1] - (40 if _VERSION_WORDS.search(h['album']) else 0))
        prefix,_=paired_title(query,best['album'])
        return 'album', ' '.join(fold(query).split()[len(prefix.split()):]), best
    if songs:
        best = max(songs,key=lambda h:affinity(query,h['title']) - (80 if _VERSION_WORDS.search(h['title']+' '+h['album']) else 0))
        # Title == album is ambiguous: resolve album groups too, not a minor
        # artist named after the song. Version hints are only soft evidence.
        return ('album' if affinity(query,best['album'])==100 else 'song'), best['title'], best
    return 'auto', query, None


def credit_names(row):
    return [credit(row)] + [c.get('name','') for c in credits(row)]


async def _run(user, query, metadata, album_metadata, intent_override='auto'):
    # Retrieval never waits for requester album/track verification. Candidate
    # MBIDs are resolved here; the separate verify endpoint gates request buttons.
    hints = [] if intent_override=='artist' else await relevance_hints(query)
    intent, target, hint = hint_intent(query,hints)
    if intent_override=='artist':intent='artist';target=query
    artists=[];albums=[];songs=[];identity=None
    if intent in ('artist','auto'):
        found=await artist_candidates(target, allow_fuzzy=(intent!='auto'))
        if found:
            row=found[0]
            artists=[{'id':row['id'],'kind':'artist','title':row['name'],'artist':row['name'],'aliases':row['_names']}]
            identity={'id':row['id'],'name':row['name'],'aliases':row['_names']}
            intent='artist'
    if not artists:
        words=safe(query).split()
        if intent=='auto':
            # Unknown intent: bounded first-pass retrieval, no discography or
            # per-candidate metadata expansion. Title evidence chooses ordering.
            group_query=' OR '.join(f'releasegroup:"{safe(" ".join(words[i:]))}"' for i in range(min(3,len(words))))
            pairs=[f'(releasegroup:"{safe(" ".join(words[i:]))}" AND artist:"{safe(" ".join(words[:i]))}")' for i in range(1,min(4,len(words)))]
            if pairs:group_query+=' OR '+' OR '.join(pairs)
            group_data,recording_data=await asyncio.gather(
                mb.mb_get('release-group',{'query':group_query,'limit':8}),
                mb.mb_get('recording',{'query':f'recording:"{safe(query)}"','limit':8}))
        elif intent=='album':
            group_query=f'releasegroup:"{safe(target)}"'
            if hint:
                artist=re.split(r'\s*[&,]\s*',hint['artist'])[0]
                group_query+=f' AND artist:"{safe(artist)}"'
            group_data=await mb.mb_get('release-group',{'query':group_query,'limit':8})
            recording_data={}
        else:
            group_data={}
            artist=re.split(r'\s*[&,]\s*',(hint or {}).get('artist',''))[0]
            search=f'recording:"{safe(target)}"'
            if artist:search+=f' AND artist:"{safe(artist)}"'
            recording_data=await mb.mb_get('recording',{'query':search,'limit':8})
        groups=group_data.get('release-groups') or []
        checked_prefixes={}
        for row in groups:
            if not valid(row.get('id')):continue
            prefix,pair=paired_title(query,row.get('title'))
            score=affinity(query,row.get('title'))
            if prefix:
                direct=any(artist_affinity(prefix,name)>=91 for name in credit_names(row))
                if not direct:
                    if prefix not in checked_prefixes:
                        checked_prefixes[prefix]=await artist_candidates(prefix)
                    for candidate in checked_prefixes[prefix][:1]:
                        related={candidate['id']}|{r.get('artist',{}).get('id') for r in candidate.get('relations',[]) if r.get('type') in ('member of band','collaboration') and isinstance(r.get('artist'),dict)}
                        direct=any(c.get('id') in related for c in credits(row))
                        if direct:break
                if direct:score=pair
            if score < 91:continue
            penalty=0 if _VERSION_WORDS.search(query) else (60 if _VERSION_WORDS.search(row.get('title','')) else 0)
            albums.append(group_item(row,score-penalty))
        for row in recording_data.get('recordings',[]):
            if row.get('video') is True:continue
            if not valid(row.get('id')) or affinity(target,row.get('title'))<91 or not credits(row):continue
            releases=[r for r in row.get('releases',[]) if r.get('status')=='Official' and valid((r.get('release-group') or {}).get('id'))]
            releases.sort(key=lambda r:(bool(_VERSION_WORDS.search(r.get('title',''))), (r.get('release-group') or {}).get('primary-type')!='Album',str(r.get('date') or '9999')))
            if not releases:continue
            release=releases[0]
            penalty=0 if _VERSION_WORDS.search(query) else (60 if _VERSION_WORDS.search(row.get('title','')+' '+release.get('title','')) else 0)
            score=affinity(target,row.get('title'))-penalty
            if hint and any(fold(n)==fold(hint['artist']) for n in credit_names(row)):score+=10
            songs.append({'id':row['id'],'kind':'song','title':row['title'],'artist':credit(row),'album':release.get('title',''),'albumId':release['release-group']['id'],'cover':'https://coverartarchive.org/release-group/'+release['release-group']['id']+'/front-500','_rank':score,'requestable':False})
        if albums:
            best=max(r['_rank'] for r in albums)
            albums=[r for r in albums if r['_rank']>=best-3]
        albums=unique(albums,'album',3)
        songs=unique(songs,'song',3)
        if intent=='auto':intent='album' if albums and not songs else 'song'
    if identity is None and (albums or songs):
        primary=(albums or songs)[0]
        identity={'id':'','name':primary['artist'],'aliases':[primary['artist']]}
    for row in albums+songs:
        row['requestable']=False
        row['verificationPending']=True
    return {'artists':artists,'albums':albums,'songs':songs,'intent':intent,'identity':identity,
            'matches':{'artists':[r['id'] for r in artists],'albums':[r['id'] for r in albums],'songs':[r['id'] for r in songs]},
            'effectiveQuery':target,'status':{}}


async def verify(user, kind, item_id, album_id, album_metadata):
    if kind=='artist':return {'requestable':False}
    group=item_id if kind=='album' else album_id
    if not valid(group) or not valid(item_id):return {'requestable':False}
    data=await album_metadata(user,group)
    tracks=data.get('tracks') or []
    valid_album=bool(data.get('title') and data.get('artist_name') and 1<=len(tracks)<=1000)
    match=next((t for t in tracks if t.get('recording_id')==item_id),None)
    return {'requestable':bool(valid_album and (kind=='album' or match))}


async def search(user, query, metadata, album_metadata, intent="auto", request=None):
    key=(user['id'],fold(query),intent)
    hit=_cache.get(key)
    if hit and time.monotonic()-hit[0]<600:
        _cache.move_to_end(key);return hit[1]
    if key not in _tasks:
        async def load():
            result=await _run(user,query,metadata,album_metadata,intent)
            _cache[key]=(time.monotonic(),result)
            while len(_cache)>128:_cache.popitem(last=False)
            return result
        task=asyncio.create_task(load());_tasks[key]=task
        def done(task):
            if _tasks.get(key) is task:_tasks.pop(key,None)
            if not task.cancelled():task.exception()
        task.add_done_callback(done)
    task=_tasks[key]
    _waiters[key]=_waiters.get(key,0)+1
    try:
        if request is None:return await asyncio.shield(task)
        while not task.done():
            await asyncio.wait({task},timeout=.1)
            if await request.is_disconnected():raise asyncio.CancelledError()
        return task.result()
    finally:
        _waiters[key]-=1
        if not _waiters[key]:
            _waiters.pop(key,None)
            if not task.done():
                if _tasks.get(key) is task:_tasks.pop(key,None)
                task.cancel()
