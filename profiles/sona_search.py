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
            response=await client.get('https://itunes.apple.com/search',params={'term':query,'media':'music','entity':'song','limit':5,'country':'US'})
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


async def artist_candidates(query):
    text=safe(query)
    data=await mb.mb_get('artist',{'query':f'artist:"{text}" OR alias:"{text}"','limit':12})
    rows=data.get('artists') or []
    candidates=[]
    for row in rows:
        if not valid(row.get('id')):continue
        names=[row.get('name')]+[a.get('name') for a in row.get('aliases',[]) if isinstance(a,dict)]
        score=max((affinity(query,n) for n in names),default=0)
        if score:candidates.append((score,row))
    if not candidates:
        # Alias arrays can be omitted from search hits. Verify the actual entity
        # before accepting a high-scoring alias hit or attempting fuzzy expansion.
        for row in rows[:2]:
            if valid(row.get('id')) and int(row.get('score') or 0)>=95:
                details=await mb.mb_get('artist/'+row['id'],{'inc':'aliases+artist-rels'})
                names=[details.get('name','')]+[a.get('name','') for a in details.get('aliases',[]) if isinstance(a,dict)]
                score=max((affinity(query,n) for n in names),default=0)
                if score:candidates.append((score,{**row,'aliases':details.get('aliases',[])}))
    if not candidates and len(fold(query))>=6:
        tokens=safe(query).split()[:4]
        fuzzy=' OR '.join(' AND '.join(f'{field}:{token}~0.65' for token in tokens) for field in ('artist','alias'))
        data=await mb.mb_get('artist',{'query':fuzzy,'limit':8})
        for row in data.get('artists',[]):
            if not valid(row.get('id')):continue
            score=max([affinity(query,row.get('name'))]+[affinity(query,a.get('name')) for a in row.get('aliases',[]) if isinstance(a,dict)])
            if score:candidates.append((score,row))
    if not candidates and len(text.split())>1:
        words=text.split()
        prefix=' AND '.join('artist:'+w+('*' if i==len(words)-1 else '') for i,w in enumerate(words))
        data=await mb.mb_get('artist',{'query':prefix,'limit':6})
        for row in data.get('artists',[]):
            score=affinity(query,row.get('name'))
            if score>=91 and valid(row.get('id')):candidates.append((score,row))
    candidates.sort(key=lambda x:(x[0],int(x[1].get('score') or 0)),reverse=True)
    # Verify aliases on real artist entities; search engine alias hits alone do
    # not establish identity. Bounded to two convincing candidates.
    verified=[]
    for score,row in candidates[:2]:
        details=await mb.mb_get('artist/'+row['id'],{'inc':'aliases+artist-rels'})
        aliases=[a.get('name','') for a in details.get('aliases',[]) if isinstance(a,dict)]
        names=[details.get('name','')]+aliases
        evidence=max((affinity(query,n) for n in names),default=0)
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


async def _run(user, query, metadata, album_metadata, intent_override="auto"):
    if intent_override=="artist":
        found=await artist_candidates(query)
        return {"artists":[{"id":row["id"],"kind":"artist","title":row["name"],"artist":row["name"],"aliases":row["_names"]} for row in found],"albums":[],"songs":[],"intent":"artist","status":{}}
    q=fold(query);words=safe(query).split()
    # Single-word titles such as Thriller are inherently ambiguous. Resolve
    # recordings and release groups alongside artist names before choosing intent.
    suffixes=[' '.join(words[i:]) for i in range(min(3,len(words)))]
    hints=await relevance_hints(query)
    hint=next((h for h in hints if affinity(query,h['title'])>=91 or affinity(query,h['artist'])>=87 or paired_title(query,h['album'])[1]),None)
    hint_artist=(hint or {}).get('artist','')
    primary_hint_artist=re.split(r'\s*[&,]\s*',hint_artist)[0]
    group_query=' OR '.join('releasegroup:"'+suffix+'"' for suffix in suffixes if suffix)
    if hint and affinity(query,hint['title'])>=91:
        recording_query=f'recording:"{safe(hint["title"])}" AND artist:"{safe(primary_hint_artist)}"'
        if affinity(query,hint['album'])>=91:group_query=f'releasegroup:"{safe(hint["album"])}" AND artist:"{safe(primary_hint_artist)}"'
    else:
        recording_query=f'recording:"{safe(query)}"'
        if hint and paired_title(query,hint['album'])[1]:group_query=f'releasegroup:"{safe(hint["album"])}" AND artist:"{safe(primary_hint_artist)}"'
    if len(words)>1 and not (hint and (affinity(query,hint['artist'])>=87 or affinity(query,hint['title'])>=91)):
        pairs=[f'(releasegroup:"{safe(" ".join(words[i:]))}" AND artist:"{safe(" ".join(words[:i]))}")' for i in range(1,min(4,len(words)))]
        group_query='('+group_query+') OR '+' OR '.join(pairs)
    artists,groups_data,recordings=await asyncio.gather(
        artist_candidates(query),
        mb.mb_get('release-group',{'query':group_query,'limit':12}),
        mb.mb_get('recording',{'query':recording_query,'limit':12}))
    group_rows=groups_data.get('release-groups') or []
    hinted_group_rows=[]
    if hint and affinity(query,hint['title'])>=91 and hint.get('album'):
        extra=await mb.mb_get('release-group',{'query':f'releasegroup:"{safe(hint["album"])}" AND artist:"{safe(primary_hint_artist)}"','limit':5})
        hinted_group_rows=[g for g in extra.get('release-groups',[]) if fold(g.get('title'))==fold(hint['album']) and artist_matches(g.get('artist-credit'),primary_hint_artist)]

    exact_groups=[g for g in group_rows if affinity(query,g.get('title'))==100 and g.get('primary-type')=='Album']
    exact_songs=[r for r in recordings.get('recordings',[]) if same_recording(query,r.get('title'))]
    artist=artists[0] if artists else None
    # A convincing long artist name/correction dominates title coincidences;
    # short artist/title collisions remain song/album searches.
    artist_intent=bool(artist and (affinity(query,hint_artist)>=87 or artist['_rank']==87 or not(exact_groups or exact_songs)))
    intent='artist' if artist_intent else 'album' if exact_groups else 'song' if exact_songs else 'album'
    album_rows=[];song_rows=[];artist_rows=[];identity=None
    if artist_intent:
        identity={'id':artist['id'],'name':artist['name'],'aliases':artist['_names']}
        artist_rows=[{'id':artist['id'],'kind':'artist','title':artist['name'],'artist':artist['name'],'_rank':artist['_rank']}]
        # Small preview only; artist pages retain their full discography.
        if not hints:hints=await relevance_hints(artist['name'])
        hinted_albums=list(dict.fromkeys(h['album'] for h in hints if affinity(h['artist'],artist['name'])>=91 and h['album']))[:3]
        if hinted_albums:
            batches=await asyncio.gather(*(mb.mb_get('release-group',{'query':f'releasegroup:"{safe(title)}" AND arid:{artist["id"]}','limit':5}) for title in hinted_albums))
            own_rows=[(row,100-index) for index,batch in enumerate(batches) for row in batch.get('release-groups',[]) if fold(row.get('title'))==fold(hinted_albums[index])]
        else:
            own=await mb.mb_get('release-group',{'artist':artist['id'],'type':'album','inc':'artist-credits','limit':12})
            own_rows=[(row,80) for row in own.get('release-groups',[])]
        for row,rank in own_rows:
            if valid(row.get('id')) and any(c.get('id')==artist['id'] for c in credits(row)):
                secondary=set(row.get('secondary-types') or [])
                if secondary.intersection({'Compilation','Live','Remix','DJ-mix','Soundtrack'}):continue
                album_rows.append(group_item(row,rank))
        popular=await metadata(user,'/api/v1/artists/'+artist['id']+'/top-songs',{'count':3})
        for row in popular.get('songs',[]):
            if valid(row.get('recording_mbid')):
                song_rows.append({'id':row['recording_mbid'],'kind':'song','title':row.get('title',''),'artist':row.get('artist_name') or artist['name'],'album':row.get('release_name') or '', 'albumId':row.get('release_group_mbid') or '', 'requestable':False,'_rank':80})
    else:
        # Artist+album phrases are resolved as a pair. A shared word is never
        # sufficient; accept the full title plus a verified credited identity.
        combo=[]
        for row in group_rows:
            prefix,pair_score=paired_title(query,row.get('title'))
            if not prefix:continue
            direct=any(affinity(prefix,c.get('name'))==100 for c in credits(row))
            if direct:combo.append((row,pair_score));continue
            identities=await artist_candidates(prefix)
            for ident in identities[:1]:
                related={ident['id']}|{r.get('artist',{}).get('id') for r in ident.get('relations',[]) if r.get('type') in ('member of band','collaboration') and isinstance(r.get('artist'),dict)}
                if any(c.get('id') in related for c in credits(row)):
                    combo.append((row,pair_score));break
        if combo:
            intent='album'
            best=max(score for _,score in combo)
            album_rows=[group_item(row,score) for row,score in combo if score>=best-3]
        else:
            for row in group_rows:
                score=affinity(query,row.get('title'))
                if score>=91 and valid(row.get('id')) and (not hint or not primary_hint_artist or artist_matches(row.get('artist-credit'),primary_hint_artist)):album_rows.append(group_item(row,score+int(row.get('score') or 0)/100))
            for row in recordings.get('recordings',[]):
                score=affinity(query,row.get('title'))
                if score<91 or not valid(row.get('id')) or not credits(row):continue
                releases=[r for r in row.get('releases',[]) if r.get('status')=='Official' and valid((r.get('release-group') or {}).get('id'))]
                releases.sort(key=lambda r:((r.get('release-group') or {}).get('primary-type')!='Album',str(r.get('date') or '9999')))
                release=next(iter(releases),{})
                gid=(release.get('release-group') or {}).get('id','')
                song_rows.append({'id':row['id'],'kind':'song','title':row['title'],'artist':credit(row),'album':release.get('title',''),'albumId':gid,'requestable':False,'_rank':score+int(row.get('score') or 0)/100,'_raw':row})
        # Artist cards are identities, not artists whose names happen to contain
        # a word in an album/song query. Do not pad this section.
    if hint and affinity(query,hint['title'])>=91:
        original_groups=hinted_group_rows or [g for g in group_rows if artist_matches(g.get('artist-credit'),primary_hint_artist)]
        preferred=[]
        for group in original_groups[:2]:
            if not valid(group.get('id')):continue
            try:
                data=await album_metadata(user,group['id'])
                if not artist_matches(group.get('artist-credit'),primary_hint_artist):continue
                for track in data.get('tracks',[]):
                    if valid(track.get('recording_id')) and same_recording(hint['title'],track.get('title')):
                        preferred.append({'id':track['recording_id'],'kind':'song','title':track['title'],'artist':data.get('artist_name') or primary_hint_artist,'album':data.get('title') or group['title'],'albumId':group['id'],'requestable':False,'_rank':200})
            except Exception:pass
        if preferred:song_rows=preferred
    # Verify only the small visible selection using the very metadata/track list
    # the existing request endpoint accepts. Cache shared album lookups.
    selected_albums=unique(album_rows,'album',3)
    for item in selected_albums:
        try:
            data=await album_metadata(user,item['id'])
            count=len(data.get('tracks') or [])
            item['requestable']=bool(1<=count<=1000 and data.get('title') and data.get('artist_name'))
        except Exception:
            item['requestable']=False
    selected_songs=unique(song_rows,'song',3)
    for item in selected_songs:
        gid=item.get('albumId')
        if not valid(gid):continue
        try:
            data=await album_metadata(user,gid)
            match=next((r for r in data.get('tracks',[]) if r.get('recording_id')==item['id'] and same_recording(item['title'],r.get('title'))),None)
            if match and fold(data.get('artist_name'))==fold(item['artist']):
                item['requestable']=True
                item['album']=data.get('title') or item.get('album','')
                item['artist']=data.get('artist_name') or item['artist']
        except Exception:pass
        item['cover']='https://coverartarchive.org/release-group/'+gid+'/front-500'
    # Unverified recording variants must not become dead request buttons.
    selected_songs=[row for row in selected_songs if row.get('requestable')]
    selected_albums=[row for row in selected_albums if row['requestable']]
    if identity is None and (selected_albums or selected_songs):
        selected_ids={row['id'] for row in selected_albums}
        source=next((row for row in group_rows+hinted_group_rows if row.get('id') in selected_ids),None)
        if source is None and hinted_group_rows:source=hinted_group_rows[0]
        if source and credits(source):
            names=[credit(source)]+[part.get('name','') for part in credits(source)]
            identity={'id':credits(source)[0].get('id',''),'name':names[0],'aliases':list(dict.fromkeys(names))}
    matches={'artists':[row['id'] for row in artist_rows],'albums':[row['id'] for row in selected_albums],'songs':[row['id'] for row in selected_songs]}

    return {'artists':unique(artist_rows,'artist',2),'albums':selected_albums,'songs':selected_songs,'intent':intent,'identity':identity,'matches':matches,'effectiveQuery':artist['name'] if artist_intent else query,'status':{}}


async def search(user, query, metadata, album_metadata, intent="auto"):
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
            _tasks.pop(key,None)
            if not task.cancelled():task.exception()
        task.add_done_callback(done)
    return await asyncio.shield(_tasks[key])
