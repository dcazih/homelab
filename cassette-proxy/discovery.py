# CASSETTE_SMART_SEARCH_V5
# CASSETTE_SMART_SEARCH_V4
# CASSETTE_ARTIST_IDENTITY_V3
"""DroppedNeedle MusicBrainz discovery -> Cassette search3 results.

DroppedNeedle's search API indexes albums/artists, not free-form recordings.
We supplement it with MusicBrainz recordings, using their canonical recording
MBIDs so DroppedNeedle can request the exact track when starred.
"""
import asyncio
import logging
import os
import re
import sqlite3
import time
import uuid
import unicodedata
from urllib.parse import quote

import httpx
from fastapi.responses import JSONResponse
from personal import PersonalizationError, credential_params, request_params, nav_json, base_envelope, error_response, to_list
from collection import connect
import music_search_upgrade as music_smart

LOG=logging.getLogger('cassette_discovery')
DN=os.getenv('DROPPEDNEEDLE_URL','http://droppedneedle:8688').rstrip('/')
# No shared DroppedNeedle token: Bridge selects the validated profile's session.
BRIDGE=os.getenv('MUSIC_BRIDGE_URL','http://music-profiles:8765').rstrip('/')
BRIDGE_KEY=os.getenv('CASSETTE_BRIDGE_INTERNAL_KEY','')
UA=os.getenv('MUSICBRAINZ_USER_AGENT','CassetteFamilyProxy/1.0 (https://github.com/CassetteLab/cassette)')
PREFIX='cassette-request-track_'
MBID=re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',re.I)
CACHE_TTL=900
# Keep typed search responsive even if an optional catalog is degraded.
REMOTE_SEARCH_BUDGET=5.0
_mb_lock=asyncio.Lock()
_last_mb_call=0.0
_cache={}
# Per-profile discovery cache; never mix accounts or return another user's catalog.
_remote_cache={}
_remote_jobs={}
REMOTE_CACHE_TTL=600


def setup():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS discovery (
            id TEXT PRIMARY KEY, recording_mbid TEXT NOT NULL UNIQUE,
            artist TEXT NOT NULL, title TEXT NOT NULL, album TEXT,
            duration INT NOT NULL DEFAULT 0, created INT NOT NULL)''')
        cols={r[1] for r in db.execute('PRAGMA table_info(discovery)')}
        if 'cover_id' not in cols:
            db.execute("ALTER TABLE discovery ADD COLUMN cover_id TEXT NOT NULL DEFAULT ''")
        if 'cover_fallback_id' not in cols:
            db.execute("ALTER TABLE discovery ADD COLUMN cover_fallback_id TEXT NOT NULL DEFAULT ''")
        db.execute('''CREATE TABLE IF NOT EXISTS discovery_albums (
            id TEXT PRIMARY KEY, mbid TEXT, name TEXT NOT NULL, artist TEXT NOT NULL,
            artist_id TEXT NOT NULL DEFAULT '', cover_id TEXT NOT NULL DEFAULT '',
            year INT, song_count INT NOT NULL DEFAULT 0, created INT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS discovery_artists (
            id TEXT PRIMARY KEY, mbid TEXT, name TEXT NOT NULL,
            cover_id TEXT NOT NULL DEFAULT '', album_count INT NOT NULL DEFAULT 1,
            created INT NOT NULL)''')


def is_external_id(value):
    return isinstance(value,str) and value.startswith(PREFIX)


def normalized(t):
    # Case, punctuation and accent insensitive, but preserves word boundaries.
    decomposed=unicodedata.normalize('NFKD', str(t or '').casefold())
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in decomposed if not unicodedata.combining(c))))


def relevance(song, query):
    """Rank recording/title and artist relevance, not accidental substrings.

    Song searches give exact recording titles priority; a one-word query may
    also be an artist search, so exact artist names get substantial weight.
    """
    q=normalized(query)
    title=normalized(song.get('title'))
    artist=normalized(song.get('artist'))
    album=normalized(song.get('album'))
    if not q:return -1000
    qwords=q.split()
    twords=title.split()
    awords=artist.split()
    score=0
    if title==q:score+=200
    elif title.startswith(q+' '):score+=135
    elif (' '+q+' ') in (' '+title+' '):score+=105
    elif all(w in twords for w in qwords):score+=78
    elif all(any(w==x for x in twords+awords) for w in qwords):score+=35
    if artist==q:score+=245 if len(qwords)==1 else 165
    elif artist.startswith(q+' '):score+=100
    elif (' '+q+' ') in (' '+artist+' '):score+=72
    elif all(w in awords for w in qwords):score+=55
    if album==q:score+=50
    source_score=song.get('_musicbrainz_score')
    if source_score is not None:
        try:score+=max(0,min(int(source_score),100)) * .20
        except (TypeError,ValueError):pass
    # Multiple names mashed into one artist credit are less strong evidence.
    if len(awords)>8 and len(qwords)==1 and artist!=q:score-=12
    return score


def is_exceptional(song, query):
    # Avoid filling the first screen with unrelated one-word song titles when
    # searching for an artist, e.g. a search for "Drake".
    return len(normalized(query).split())>=2 and normalized(song.get('title'))==normalized(query)


def matching(text,query):
    words=normalized(query).split()
    tokens=set(normalized(text).split())
    return bool(words) and all(w in tokens for w in words)


def clean(t,maxlen=200):
    return re.sub(r'[\x00-\x1f\x7f]','',str(t or ''))[:maxlen].strip()


def add_candidate(mbid,artist,title,album='',duration=0,release_mbid='',release_group_mbid='',musicbrainz_score=None):
    if not MBID.fullmatch(str(mbid)) or not title or not artist:
        return None
    from cover_art import cover_id_for
    cover_id=cover_id_for(release_mbid,release_group_mbid)
    cover_fallback=cover_id_for('',release_group_mbid) if release_mbid else ''
    item_id=PREFIX+str(uuid.uuid5(uuid.NAMESPACE_URL,'musicbrainz-recording:'+mbid.lower()))
    with connect() as db:
        # Keep known artwork on repeat searches where a source omits releases.
        db.execute('''INSERT INTO discovery (id,recording_mbid,artist,title,album,duration,created,cover_id,cover_fallback_id)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            artist=excluded.artist,title=excluded.title,album=excluded.album,duration=excluded.duration,
            cover_id=CASE WHEN excluded.cover_id!='' THEN excluded.cover_id ELSE discovery.cover_id END,
            cover_fallback_id=CASE WHEN excluded.cover_fallback_id!='' THEN excluded.cover_fallback_id ELSE discovery.cover_fallback_id END''',
            (item_id,mbid.lower(),clean(artist),clean(title),clean(album),int(duration or 0),int(time.time()),cover_id,cover_fallback))
        row=db.execute('SELECT cover_id FROM discovery WHERE id=?',(item_id,)).fetchone()
        cover_id=row[0] if row else cover_id
    song=external_song({'id':item_id,'title':clean(title),'artist':clean(artist),
                          'album':clean(album),'duration':int(duration or 0),
                          'recording_mbid':mbid.lower(),'cover_id':cover_id})
    if musicbrainz_score is not None:song['_musicbrainz_score']=musicbrainz_score
    return song


async def dn_json(client, username, operation, *, query='', album_mbid=''):
    if not BRIDGE_KEY or not username:
        return None
    try:
        if operation=='albums':
            path='/v1/internal/cassette/dn/search/albums'
            body={'username':username,'query':query,'limit':10}
        elif operation=='catalog':
            path='/v1/internal/cassette/dn/search'
            body={'username':username,'query':query,'limit':15}
        elif operation=='tracks' and MBID.fullmatch(album_mbid):
            path='/v1/internal/cassette/dn/album-tracks'
            body={'username':username,'album_mbid':album_mbid}
        else:
            return None
        response=await client.post(f'{BRIDGE}{path}',json=body,
            headers={'X-Cassette-Internal-Key':BRIDGE_KEY},timeout=4.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError,ValueError) as exc:
        LOG.warning('DroppedNeedle bridge catalog lookup failed: %s', type(exc).__name__)
        return None


def remote_artist_id(artist, mbid=''):
    identity=(str(mbid).lower() if MBID.fullmatch(str(mbid)) else normalized(artist))
    return 'cassette-request-artist_'+str(uuid.uuid5(uuid.NAMESPACE_URL,'musicbrainz-artist:'+identity))


def remember_remote_artist(name, mbid='', cover_id='', album_count=1):
    if not name or len(name)>200:return None
    ident=remote_artist_id(name,mbid)
    with connect() as db:
        db.execute('''INSERT INTO discovery_artists VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
            album_count=MAX(discovery_artists.album_count,excluded.album_count),
            cover_id=CASE WHEN excluded.cover_id LIKE 'cassette-art-release_%' THEN excluded.cover_id
                          WHEN discovery_artists.cover_id LIKE 'cassette-art-release_%' THEN discovery_artists.cover_id
                          WHEN excluded.cover_id!='' THEN excluded.cover_id ELSE discovery_artists.cover_id END''',
            (ident,str(mbid or ''),clean(name),str(cover_id or ''),max(1,int(album_count or 1)),int(time.time())))
    return {'id':ident,'name':clean(name),'albumCount':max(1,int(album_count or 1)),
            **({'musicBrainzId':str(mbid)} if MBID.fullmatch(str(mbid)) else {}),
            **({'coverArt':cover_id} if cover_id else {})}


def remember_remote_album(name, artist, mbid='', artist_mbid='', release_mbid='', year=None, song_count=0):
    if not name or not artist:return None
    ident=external_album_id(artist,name)
    art=__import__('cover_art').cover_id_for(release_mbid,mbid)
    artist_id=remote_artist_id(artist,artist_mbid)
    with connect() as db:
        db.execute('''INSERT INTO discovery_albums VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
            mbid=CASE WHEN excluded.mbid!='' THEN excluded.mbid ELSE discovery_albums.mbid END,
            artist_id=excluded.artist_id,
            cover_id=CASE WHEN excluded.cover_id LIKE 'cassette-art-release_%' THEN excluded.cover_id
                          WHEN discovery_albums.cover_id LIKE 'cassette-art-release_%' THEN discovery_albums.cover_id
                          WHEN excluded.cover_id!='' THEN excluded.cover_id ELSE discovery_albums.cover_id END,
            song_count=MAX(discovery_albums.song_count,excluded.song_count),
            year=COALESCE(excluded.year,discovery_albums.year)''',
            (ident,str(mbid or ''),clean(name),clean(artist),artist_id,art,
             int(year) if str(year or '').isdigit() else None,int(song_count or 0),int(time.time())))
    remember_remote_artist(artist,artist_mbid,art)
    with connect() as db:
        saved=db.execute('SELECT cover_id FROM discovery_albums WHERE id=?',(ident,)).fetchone()
    best_art=(saved[0] if saved else '') or art
    result={'id':ident,'name':clean(name),'artist':clean(artist),'artistId':artist_id,
            'songCount':int(song_count or 0),'duration':0}
    if best_art:result['coverArt']=best_art
    if MBID.fullmatch(str(mbid)):result['musicBrainzId']=str(mbid)
    if year and str(year).isdigit():result['year']=int(year)
    return result


def make_artist_album_results(query, catalog, recordings):
    """Produce browseable ID3 objects from DN artist/album catalog + MB matches."""
    albums=[];artists=[]
    artist_mbids={}
    if isinstance(catalog,dict):
        # Resolve artist IDs first so albums and artist pages use the SAME ID.
        for a in (catalog.get('artists') or [])[:30]:
            if not isinstance(a,dict):continue
            name=a.get('title') or a.get('name') or ''
            mbid=a.get('musicbrainz_id','')
            if name and MBID.fullmatch(str(mbid)):
                artist_mbids[normalized(name)]=mbid
            if not matching(name,query):continue
            result=remember_remote_artist(name,mbid,album_count=a.get('album_count') or 1)
            if result:artists.append(result)
        for a in (catalog.get('albums') or [])[:30]:
            if not isinstance(a,dict):continue
            artist=a.get('artist') or a.get('artist_name') or ''
            name=a.get('title') or ''
            if not matching(artist+' '+name,query):continue
            artist_mbid=a.get('artist_id') or artist_mbids.get(normalized(artist),'')
            result=remember_remote_album(name,artist,a.get('musicbrainz_id',''),artist_mbid,
                        year=a.get('year'),song_count=a.get('track_count') or 0)
            if result:albums.append(result)
    for song in recordings[:25]:
        # MB release metadata can seed browseable albums and artist cards.
        album=song.get('album')
        artist=song.get('artist')
        if album and artist and matching(album+' '+artist,query):
            found=remember_remote_album(album,artist,artist_mbid=artist_mbids.get(normalized(artist),''),song_count=1)
            if found:albums.append(found)
        if artist and matching(artist,query):
            found=remember_remote_artist(artist,artist_mbids.get(normalized(artist),''))
            if found:artists.append(found)
    def unique(items):
        seen=set();out=[]
        for item in items:
            if item['id'] not in seen:seen.add(item['id']);out.append(item)
        return out
    return unique(artists)[:15],unique(albums)[:20]


async def droppedneedle_tracks(client, query, username):
    """DN search/album tracks use each Navidrome user's linked DN identity."""
    data=await dn_json(client,username,'albums',query=query)
    albums=data.get('results',[]) if isinstance(data,dict) else []
    if not isinstance(albums,list):return []
    albums=[a for a in albums if isinstance(a,dict) and MBID.fullmatch(str(a.get('musicbrainz_id','')))][:6]
    async def tracks(album):
        ident=album['musicbrainz_id']
        result=await dn_json(client,username,'tracks',album_mbid=ident)
        batch=result.get('tracks',[]) if isinstance(result,dict) else []
        songs=[]
        for tr in batch[:100] if isinstance(batch,list) else []:
            if not isinstance(tr,dict) or not matching(' '.join((tr.get('title') or '', album.get('artist') or '', album.get('title') or '')),query):
                continue
            entry=add_candidate(tr.get('recording_id'),album.get('artist'),tr.get('title'),album.get('title'),
                                int((tr.get('length') or 0)/1000),
                                release_mbid=result.get('selected_release_mbid','') if isinstance(result,dict) else '',
                                release_group_mbid=ident)
            if entry:songs.append(entry)
        return songs
    batches=await asyncio.gather(*(tracks(a) for a in albums[:3]))
    return [song for batch in batches for song in batch]


async def mb_recordings(client,query):
    """MusicBrainz API permits one request per second per IP and requires UA."""
    global _last_mb_call
    query=query.strip()
    key=query.casefold()
    now=time.monotonic()
    found=_cache.get(key)
    if found and now-found[0]<CACHE_TTL:
        return found[1]
    # Do not build up a long backlog for every keystroke while MusicBrainz
    # enforces a one-request-per-second limit.
    try:
        await asyncio.wait_for(_mb_lock.acquire(),timeout=3.0)
    except asyncio.TimeoutError:
        return []
    try:
        now=time.monotonic()
        found=_cache.get(key)
        if found and now-found[0]<CACHE_TTL:
            return found[1]
        await asyncio.sleep(max(0,1.10-(time.monotonic()-_last_mb_call)))
        _last_mb_call=time.monotonic()
        try:
            res=await client.get('https://musicbrainz.org/ws/2/recording/',
                params={'query':query,'fmt':'json','dismax':'true','limit':'30'},
                headers={'User-Agent':UA,'Accept':'application/json'},timeout=3.5)
            res.raise_for_status()
            data=res.json()
        except (httpx.HTTPError,ValueError) as exc:
            LOG.warning('MusicBrainz recording lookup failed: %s', type(exc).__name__)
            return []
        results=[]
        for rec in data.get('recordings',[]):
            if not isinstance(rec,dict):continue
            artist=clean(rec.get('artist-credit-phrase') or ' '.join(
                c.get('name','') for c in rec.get('artist-credit',[]) if isinstance(c,dict)))
            title=clean(rec.get('title'))
            if not matching(f'{artist} {title}',query):
                continue
            album=''
            releases=rec.get('releases') or []
            if releases and isinstance(releases[0],dict):album=clean(releases[0].get('title'))
            result=add_candidate(rec.get('id'),artist,title,album,round((rec.get('length') or 0)/1000),
                                  release_mbid=releases[0].get('id','') if releases and isinstance(releases[0],dict) else '',
                                  release_group_mbid=(releases[0].get('release-group') or {}).get('id','') if releases and isinstance(releases[0],dict) else '',
                                  musicbrainz_score=rec.get('score'))
            if result:
                if album and artist and releases and isinstance(releases[0],dict):
                    release=releases[0]
                    remember_remote_album(album,artist,mbid=(release.get('release-group') or {}).get('id',''),release_mbid=release.get('id',''),song_count=1)
                results.append(result)
            if len(results)>=30:break
        _cache[key]=(time.monotonic(),results)
        if len(_cache)>300:_cache.clear()
        return results
    finally:
        _mb_lock.release()


def _search_key(username,query):
    return (username.casefold(),normalized(query))


async def _load_external(client,username,query,key,allow_spell=True):
    # For a verified exact artist name or alias, browse by the artist's MBID,
    # never by strings embedded in tribute-act or mashup artist names.
    from artist_identity import artist_search
    identity_result = await artist_search(__import__('discovery'),client,username,query)
    if identity_result is not None:
        songs,artists,albums,identity=identity_result
        _remote_cache[key]=(time.monotonic(),songs,artists,albums,
                            {'intent':'artist','identity':identity})
        LOG.info('Completed identity-verified artist search: songs=%d albums=%d',len(songs),len(albums))
        return
    # No exact artist identity: fall back to regular song/album discovery.
    async def guarded(name, awaitable):
        try:
            return await asyncio.wait_for(awaitable,timeout=REMOTE_SEARCH_BUDGET)
        except (asyncio.TimeoutError,httpx.HTTPError,ValueError) as exc:
            LOG.warning('Search enrichment %s unavailable: %s',name,type(exc).__name__)
            return [] if name!='catalog' else {}
    catalog,mb=await asyncio.gather(
        guarded('catalog',dn_json(client,username,'catalog',query=query)),
        guarded('mb',mb_recordings(client,query)))
    if not isinstance(catalog,dict):catalog={}
    # Album-tracks lookup is optional and bounded, not on the critical search path.
    dn=await guarded('dn',droppedneedle_tracks_from_catalog(client,query,username,catalog))
    artists,albums=make_artist_album_results(query,catalog,mb)
    # A string match alone is insufficient evidence that an artist page exists.
    artists=[]
    dedup=set(); songs=[]
    for remote in (dn or [])+(mb or []):
        k=(normalized(remote.get('artist')),normalized(remote.get('title')))
        if k in dedup:continue
        dedup.add(k);songs.append(remote)
    songs.sort(key=lambda x: relevance(x,query),reverse=True)
    _remote_cache[key]=(time.monotonic(),songs[:45],artists,albums)
    # Only correct empty $ searches. Any suggested spelling must lead to
    # real catalog results, so arbitrary dictionary rewrites cannot fabricate
    # artist/album/song identities or override existing correct matches.
    if allow_spell and not __import__('artist_enrichment_v5').credible_match(
            __import__('discovery'),query,songs,artists,albums):
        try:
            suggestions=await music_smart.spelling_candidates(
                __import__('discovery'),client,query)
            for suggestion in suggestions[:5]:
                corrected_key=_search_key(username,suggestion)
                if corrected_key==key:continue
                saved=_remote_cache.get(corrected_key)
                if saved is None:
                    await _load_external(client,username,suggestion,corrected_key,
                                         allow_spell=False)
                    saved=_remote_cache.get(corrected_key)
                if saved and ((len(saved)>4 and saved[4].get('intent')=='artist') or
                    __import__('artist_enrichment_v5').credible_match(
                        __import__('discovery'),suggestion,saved[1],saved[2],saved[3])):
                    _remote_cache[key]=(time.monotonic(),*saved[1:])
                    LOG.info('Verified typo correction: %s -> %s',
                             normalized(query),normalized(suggestion))
                    break
        except Exception as exc:
            LOG.warning('Spelling fallback failed safely: %s',type(exc).__name__)
    if len(_remote_cache)>250:
        # Drop stale metadata rather than retaining a global cross-user cache.
        for k in list(_remote_cache)[:100]:_remote_cache.pop(k,None)
    LOG.info('Completed remote search: songs=%d artists=%d albums=%d',len(songs),len(artists),len(albums))


async def droppedneedle_tracks_from_catalog(client,query,username,catalog):
    albums=catalog.get('albums',[]) if isinstance(catalog,dict) else []
    if not isinstance(albums,list):return []
    albums=[a for a in albums if isinstance(a,dict) and MBID.fullmatch(str(a.get('musicbrainz_id','')))][:4]
    async def tracks(album):
        data=await dn_json(client,username,'tracks',album_mbid=album['musicbrainz_id'])
        if not isinstance(data,dict):return []
        songs=[]
        for tr in (data.get('tracks') or [])[:60]:
            if not isinstance(tr,dict):continue
            if not matching(f"{tr.get('title','')} {album.get('artist','')} {album.get('title','')}",query):continue
            entry=add_candidate(tr.get('recording_id'),album.get('artist'),tr.get('title'),album.get('title'),
                    int((tr.get('length') or 0)/1000),
                    release_mbid=data.get('selected_release_mbid',''),release_group_mbid=album['musicbrainz_id'])
            if entry:songs.append(entry)
        return songs
    batches=await asyncio.gather(*(tracks(a) for a in albums[:3]))
    return [song for batch in batches for song in batch]


def ranked_entities(local, external, query, kind):
    """Rank exact names ahead of prefixes and weak matches. Stable local-first ties."""
    q=normalized(query)
    tokens=q.split()
    seen=set()
    entries=[]
    for item in list(local)+list(external):
        if not isinstance(item,dict):continue
        name=normalized(item.get('name'))
        if not name:continue
        artist=normalized(item.get('artist')) if kind=='album' else ''
        key=(name,artist) if kind=='album' else (name,)
        if key in seen:continue
        seen.add(key)
        words=set(name.split())
        if name==q:score=1000
        elif name.startswith(q+' '):score=600
        elif q in name:score=350
        elif tokens and all(t in words for t in tokens):score=250
        else:score=0
        if artist==q:score+=120
        elif artist.startswith(q+' '):score+=55
        entries.append((score,item))
    entries.sort(key=lambda pair:pair[0],reverse=True)
    return [item for _,item in entries[:5]]


async def search_response(request,upstream,body):
    # CASSETTE_DOLLAR_SEARCH_V1: plain=local fast; $=wait for external in SAME response.
    try:
        params=request_params(request,body)
        if (params.get('f') or 'xml').lower()!='json':
            return error_response('External search requires f=json')
        query=(params.get('query') or '').strip()
        external_mode=query.startswith('$')
        if external_mode:
            query=query[1:].strip()
            # Don't send a bare '$' to Navidrome as a catalog query.
            if len(query)<2 or len(query)>160:
                return error_response('Type at least two characters after $',70,status_code=400)
        elif not query or len(query)<2 or len(query)>160:
            return None
        auth=credential_params(params)
        local=await nav_json(request.app.state.client,upstream,'search3',auth,
            {'query':query,'songCount':'75','albumCount':'20','artistCount':'20'})
        raw=local.get('searchResult3',{})
        if not isinstance(raw,dict):raise PersonalizationError('Invalid Navidrome search response')
        local_effective_query=query
        if not external_mode and not any(to_list(raw.get(t)) for t in ('song','album','artist')):
            names=[]
            try:
                with connect() as db:
                    for table,col in (('discovery_artists','name'),('discovery_albums','name'),('discovery','title')):
                        names += [r[0] for r in db.execute(
                            f'SELECT DISTINCT {col} FROM {table} ORDER BY created DESC LIMIT 600')]
            except sqlite3.Error:
                pass
            correction=(music_smart.best_spelling(query,names) or [None])[0]
            if correction is None:
                # Search one distinctive intact token from Navidrome itself.
                # This discovers local music vocabulary without catalog scans
                # or external provider calls on ordinary searches.
                words=[w for w in music_smart.norm(query).split()
                       if w not in music_smart.STOP and len(w)>=4]
                if len(words)>=1 and len(music_smart.norm(query).split())>=2:
                    probe=words[-1]
                    partial=await nav_json(request.app.state.client,upstream,'search3',auth,
                        {'query':probe,'songCount':'75','albumCount':'20','artistCount':'20'})
                    source_raw=partial.get('searchResult3',{})
                    if isinstance(source_raw,dict):
                        for item in (to_list(source_raw.get('song'))+
                                     to_list(source_raw.get('album'))+
                                     to_list(source_raw.get('artist'))):
                            if isinstance(item,dict):
                                names += [str(item.get(f) or '') for f in
                                          ('title','name','album','artist')]
                        correction=(music_smart.best_spelling(query,names) or [None])[0]
            if correction is None:
                correction=music_smart.symspell_candidate(query)
            if correction:
                alternative=await nav_json(request.app.state.client,upstream,'search3',auth,
                    {'query':correction,'songCount':'75','albumCount':'20','artistCount':'20'})
                alternative_raw=alternative.get('searchResult3',{})
                if isinstance(alternative_raw,dict) and any(
                        to_list(alternative_raw.get(t)) for t in ('song','album','artist')):
                    local,raw=alternative,alternative_raw
                    local_effective_query=correction
        # CASSETTE_SEARCH_ENVELOPE_FIX_V1: preserve the Navidrome response
        # independently of per-album loop variables below.
        source_envelope=dict(local)
        if source_envelope.get('status') != 'ok' or not source_envelope.get('version'):
            raise PersonalizationError('Invalid Navidrome search response')
        key=_search_key(auth['u'],query)
        # A normal search never reads an external cache or launches external work.
        cached=_remote_cache.get(key) if external_mode else None
        if cached and time.monotonic()-cached[0]>=REMOTE_CACHE_TTL:
            _remote_cache.pop(key,None);cached=None
        if external_mode and not cached:
            # Join any existing lookup for this profile/query, or launch exactly one.
            # Crucially, wait before returning search3 to the native Cassette app.
            current=_remote_jobs.get(key)
            if current is None or current.done():
                current=asyncio.create_task(_load_external(request.app.state.client,auth['u'],query,key))
                _remote_jobs[key]=current
                def complete(done):
                    if _remote_jobs.get(key) is done:_remote_jobs.pop(key,None)
                    if not done.cancelled() and done.exception():
                        LOG.error('External search failed: %s',type(done.exception()).__name__)
                current.add_done_callback(complete)
            try:
                # Shield shared work so one cancelled typed search can't cancel
                # a newer same-query search in the same account.
                await asyncio.wait_for(asyncio.shield(current),timeout=24.0)
            except asyncio.TimeoutError:
                return error_response('External search is still working; retry shortly',70,status_code=503)
            except Exception:
                LOG.exception('External search unavailable')
                return error_response('External search failed; retry',70,status_code=503)
            cached=_remote_cache.get(key)
            if cached is None:
                return error_response('External search returned no response; retry',70,status_code=503)
        local_songs=[dict(x) for x in to_list(raw.get('song')) if isinstance(x,dict)]
        local_songs.sort(key=lambda x:relevance(x,local_effective_query),reverse=True)
        local_artists=to_list(raw.get('artist'))
        local_albums=to_list(raw.get('album'))
        ext_songs=[];ext_artists=[];ext_albums=[]
        intent_identity=None
        if cached:
            _,ext_songs,ext_artists,ext_albums=cached[:4]
            meta=cached[4] if len(cached)>4 else {}
            if meta.get('intent')=='artist':
                intent_identity=meta['identity']
                wanted={normalized(query),normalized(intent_identity['canonical']),
                        normalized(intent_identity['display'])}
                wanted.update(normalized(x) for x in intent_identity.get('aliases',[]))
                # A library artist named "Kanye West Tribute Band" must NOT be
                # treated as Kanye West or grouped with Ye's catalog.
                def owned_by_artist(x):
                    ident=str(x.get('musicBrainzId') or '')
                    return (ident.lower()==intent_identity['id'] or
                            normalized(x.get('artist') or x.get('displayArtist')) in wanted)
                local_songs=[x for x in local_songs if owned_by_artist(x)]
                local_albums=[x for x in local_albums if owned_by_artist(x)]
                # The external canonical card intentionally replaces the native
                # artist card so tapping it fetches the full discography.
                local_artists=[]
                # Verified artist browse results are not ranked by an unrelated
                # album/song's containing the *artist name* in its title.
                ext_songs=list(ext_songs)
            else:
                keys={(normalized(x.get('artist')),normalized(x.get('title'))) for x in local_songs}
                ext_songs=[dict(x) for x in ext_songs if (normalized(x.get('artist')),normalized(x.get('title'))) not in keys]
                ext_songs.sort(key=lambda x:relevance(x,query),reverse=True)
        # Five external songs PLUS up to five library songs. Keep two/two
        # interleaving, but do not let library matches consume the external five.
        # Stable artist+title normalization prevents duplicates across editions.
        local_pool=[];remote_pool=[];seen=set()
        for item in local_songs:
            k=(normalized(item.get('artist')),normalized(item.get('title')))
            if not k[1] or k in seen:continue
            seen.add(k);local_pool.append(item)
            if len(local_pool)>=5:break
        for item in ext_songs:
            # For identity matches, old and new artist aliases refer to one
            # MB artist; use title-only dedup there.
            title=normalized(item.get('title'))
            k=(normalized(item.get('artist')),title)
            local_titles={normalized(x.get('title')) for x in local_pool}
            if not title or (intent_identity and title in local_titles) or k in seen:continue
            seen.add(k);remote_pool.append(item)
            if len(remote_pool)>=5:break
        songs=local_pool[:2]+remote_pool[:2]+local_pool[2:]+remote_pool[2:]
        if external_mode:
            # An external catalog album without a verified track count cannot
            # prove a local album is complete. Keep the external requestable ID
            # unless the local copy has a known sufficient track count.
            incomplete=set()
            for remote in ext_albums:
                remote_count=int(remote.get('songCount') or 0)
                for native_album in local_albums:
                    same=(normalized(remote.get('name'))==normalized(native_album.get('name')) and
                          normalized(remote.get('artist'))==normalized(native_album.get('artist')))
                    if same and (not remote_count or int(native_album.get('songCount') or 0)<remote_count):
                        incomplete.add(str(native_album.get('id')))
            local_albums=[a for a in local_albums if str(a.get('id')) not in incomplete]
        local_artist_ids={x.get('musicBrainzId') for x in local_artists if isinstance(x,dict)}
        local_album_ids={x.get('musicBrainzId') for x in local_albums if isinstance(x,dict)}
        ext_artists=[x for x in ext_artists if x.get('musicBrainzId') not in local_artist_ids or not x.get('musicBrainzId')]
        ext_albums=[x for x in ext_albums if x.get('musicBrainzId') not in local_album_ids or not x.get('musicBrainzId')]
        # Cassette displays artist and album sections separately from Songs.
        if intent_identity is not None:
            # The artist's actual discography titles need not contain the
            # artist's name. Display newest five, deduping by album title.
            album_rows=[]; seen_albums=set()
            # The authoritative MBID-scoped discography determines WHICH
            # albums appear, while a matching local copy remains playable.
            local_by_name={normalized(x.get('name')):x for x in local_albums}
            for remote in ext_albums:
                k=normalized(remote.get('name'))
                if not k or k in seen_albums:continue
                seen_albums.add(k)
                local_copy=local_by_name.get(k)
                if local_copy:
                    # Keep the real/playable Navidrome album ID but take the
                    # verified external cover/year when Navidrome lacks them.
                    merged=dict(local_copy)
                    for field in ('coverArt','year','musicBrainzId','songCount'):
                        if not merged.get(field) and remote.get(field):
                            merged[field]=remote[field]
                    album_rows.append(merged)
                else:
                    album_rows.append(remote)
                if len(album_rows)>=5:break
            artist_rows=ext_artists[:1]
        else:
            album_rows=ranked_entities(local_albums,ext_albums,local_effective_query,'album')
            artist_rows=ranked_entities(local_artists,ext_artists,local_effective_query,'artist')
            # Never mistake a name-containing tribute or collaboration for the
            # queried artist. Partial artist names remain possible for one word.
            q=normalized(local_effective_query)
            artist_rows=[a for a in artist_rows if normalized(a.get('name'))==q or
                (len(q.split())==1 and normalized(a.get('name')).startswith(q+' '))][:5]
        payload={**raw,'song':songs,'artist':artist_rows,'album':album_rows}
        from cover_art import augment_payload
        augment_payload(payload)
        from saved_favorites import mark_songs
        mark_songs(auth['u'],songs)
        for item in songs:item.pop('_musicbrainz_score',None)
        return JSONResponse({'subsonic-response':{**source_envelope,'searchResult3':payload}},headers={
            'Cache-Control':'private, no-store',
            'X-Cassette-Search-Phase':'external' if external_mode else 'library',
            'X-Cassette-External-Count':str(len(ext_songs)),
            'X-Cassette-External-Artists':str(len(ext_artists)),
            'X-Cassette-External-Albums':str(len(ext_albums)),
        })
    except PersonalizationError as exc:
        return error_response(str(exc),exc.code)
    except Exception:
        LOG.exception('Fast search adapter failed')
        return error_response('Search unavailable; retry',70,status_code=502)


async def request_external(request,upstream,auth,sid):
    """Save the user's star first. Acquisition is a separate, recoverable action."""
    from saved_favorites import save_song
    if not BRIDGE_KEY:
        # Save intent, but never claim a request was submitted.
        pending='bridge_not_configured'
    else:
        pending='bridge_unavailable'
    with connect() as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM discovery WHERE id=?',(sid,)).fetchone()
        if row is None:
            raise PersonalizationError('Search result expired; search again')
        item=dict(row)
    save_song(auth['u'],sid)
    if BRIDGE_KEY:
        try:
            res=await request.app.state.client.post(f'{BRIDGE}/v1/internal/cassette/request',
                json={'username':auth['u'],'artist':item['artist'],'title':item['title'],
                      'album':item['album'],'recording_mbid':item['recording_mbid']},
                headers={'X-Cassette-Internal-Key':BRIDGE_KEY},timeout=25)
            res.raise_for_status()
            data=res.json()
            pending=str(data.get('status') or 'bridge_unknown')
        except (httpx.HTTPError,ValueError,TypeError):
            pending='bridge_unavailable'
            LOG.warning('Favorite saved but Bridge request not confirmed for %s',sid)
            data={}
    else:
        data={}
    if pending=='added' and data.get('song_id'):
        try:
            with connect() as db:
                user=auth['u'].casefold()
                from collection import bootstrap,sync
                await bootstrap(db,request.app.state.client,upstream,auth)
                db.execute('INSERT OR IGNORE INTO individual VALUES (?,?)',(user,str(data['song_id'])))
                db.execute('DELETE FROM exclusions WHERE username=? AND song=?',(user,str(data['song_id'])))
                db.commit()
                await sync(db,request,upstream,auth)
        except Exception:
            # An import/playlist reconciliation failure must not roll back the
            # user's persisted star. It can be retried independently.
            pending='playlist_sync_needed'
            LOG.exception('Existing recording selected but collection reconciliation failed')
    request.app.state.personal_cache.clear()
    # Respond OK for a valid, persisted favorite even when acquisition needs
    # review, but report the truthful request status separately.
    return JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1','type':'navidrome'}},
        headers={'Cache-Control':'no-store','X-Cassette-Request-Status':pending})


async def expanded_external_album_detail(request,auth,ext_id):
    """Browse every available track of a known external album, not just search hits.

    Uses the same per-user DroppedNeedle bridge session as external search.
    Never attempts a download and falls back to cached metadata if unreachable.
    """
    if not ext_id.startswith('cassette-request-album_'):
        return external_detail(ext_id,'getAlbum',auth['u'])
    with connect() as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM discovery_albums WHERE id=?',(ext_id,)).fetchone()
    if row is None or not MBID.fullmatch(str(row['mbid'] or '')):
        return external_detail(ext_id,'getAlbum',auth['u'])
    data=await dn_json(request.app.state.client,auth['u'],'tracks',album_mbid=row['mbid'])
    raw=data.get('tracks') if isinstance(data,dict) else None
    if not isinstance(raw,list) or not raw:
        return external_detail(ext_id,'getAlbum',auth['u'])
    songs=[]
    for entry in raw[:500]:
        if not isinstance(entry,dict):continue
        recording=entry.get('recording_id')
        title=entry.get('title')
        if not MBID.fullmatch(str(recording or '')) or not title:continue
        song=add_candidate(recording,row['artist'],title,row['name'],
            int((entry.get('length') or 0)/1000),
            release_mbid=data.get('selected_release_mbid',''),
            release_group_mbid=row['mbid'])
        if song:
            song['track']=int(entry.get('track_number') or len(songs)+1)
            song['discNumber']=int(entry.get('disc_number') or 1)
            songs.append(song)
    if not songs:
        return external_detail(ext_id,'getAlbum',auth['u'])
    from saved_favorites import mark_songs
    mark_songs(auth['u'], songs)
    album={'id':ext_id,'name':row['name'],'artist':row['artist'],
           'songCount':len(songs),'duration':sum(s.get('duration',0) for s in songs),
           'song':songs}
    selected_release=str(data.get('selected_release_mbid') or '')
    if MBID.fullmatch(selected_release):
        from cover_art import cover_id_for
        album_art=cover_id_for(selected_release,row['mbid'])
        with connect() as db:
            db.execute('UPDATE discovery_albums SET cover_id=? WHERE id=?',(album_art,ext_id))
        album['coverArt']=album_art
    elif row['cover_id']:
        album['coverArt']=row['cover_id']
    if row['artist_id']:album['artistId']=row['artist_id']
    from album_requests_v6 import album_starred
    starred_at=album_starred(auth['u'],row['mbid'])
    if starred_at: album['starred']=starred_at
    return JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1',
        'album':album}},headers={'Cache-Control':'private, no-store'})


# CASSETTE_EXTERNAL_ARTIST_DISCOGRAPHY_V1
"""External artist discography lookup, using MusicBrainz + per-profile DroppedNeedle.
Only used by getArtist, not search. No download requests or credentials logged.
"""

# External album result pages must use the artist's real discography, not the
# tiny list of albums that happened to be cached by the previous search.
_ARTIST_LOOKUP_ALBUM_LIMIT = 200
_ARTIST_LOOKUP_TIMEOUT = 6.0
_ARTIST_MB_CACHE_TTL = 3600.0
_artist_mb_cache = {}


async def _artist_mb_get(client, path, params):
    """Use the same global MusicBrainz rate gate as recording search.

    A failed optional metadata fetch must not blank already known albums.
    """
    global _last_mb_call
    key=(path,tuple(sorted((str(k),str(v)) for k,v in params.items())))
    hit=_artist_mb_cache.get(key)
    if hit and time.monotonic()-hit[0]<_ARTIST_MB_CACHE_TTL:
        return hit[1]
    try:
        await asyncio.wait_for(_mb_lock.acquire(), timeout=8.0)
    except asyncio.TimeoutError:
        LOG.warning('Artist album metadata busy')
        return None
    try:
        await asyncio.sleep(max(0.0, 1.10 - (time.monotonic() - _last_mb_call)))
        _last_mb_call = time.monotonic()
        try:
            result = await client.get('https://musicbrainz.org/ws/2/' + path,
                params={**params, 'fmt':'json'},
                headers={'User-Agent':UA,'Accept':'application/json'},
                timeout=_ARTIST_LOOKUP_TIMEOUT)
            result.raise_for_status()
            data = result.json()
            if isinstance(data,dict):
                if len(_artist_mb_cache)>256:_artist_mb_cache.clear()
                _artist_mb_cache[key]=(time.monotonic(),data)
                return data
            return None
        except (httpx.HTTPError, ValueError) as exc:
            LOG.warning('Artist album metadata unavailable: %s',type(exc).__name__)
            return None
    finally:
        _mb_lock.release()


async def _resolve_artist_mbid(client, artist_name, existing):
    if MBID.fullmatch(str(existing or '')):
        return str(existing).lower()
    # Search by an exact quoted artist name. More than one exact artist is
    # ambiguous; do not silently merge their catalogues.
    safe_name=artist_name.replace('"','').replace('\\','').strip()
    if not safe_name:return ''
    data=await _artist_mb_get(client, 'artist/',
        {'query':'artist:"'+safe_name+'"','limit':15})
    if not data:return ''
    ids={a['id'].lower() for a in data.get('artists',[]) if isinstance(a,dict)
         and MBID.fullmatch(str(a.get('id','')))
         and normalized(a.get('name'))==normalized(artist_name)
         and int(a.get('score') or 0)>=95}
    return next(iter(ids)) if len(ids)==1 else ''


async def _browse_artist_album_groups(client, artist_mbid, max_groups=1000):
    """Browse real album release groups, paging beyond search's ten-result cap."""
    if not MBID.fullmatch(str(artist_mbid or '')):return []
    found=[]
    # Search gets a small budget; the artist detail page loads full discography.
    for offset in range(0,max_groups,100):
        data=await _artist_mb_get(client, 'release-group/',
            {'artist':artist_mbid,'type':'album|ep','limit':100,'offset':offset})
        if data is None:break
        groups=data.get('release-groups') or []
        if not isinstance(groups,list):break
        for g in groups:
            if not isinstance(g,dict):continue
            mbid=str(g.get('id') or '')
            name=str(g.get('title') or '').strip()
            if not MBID.fullmatch(mbid) or not name:continue
            if g.get('primary-type') not in (None,'Album','EP'):continue
            found.append({'title':name,'musicbrainz_id':mbid,
                         'year':str(g.get('first-release-date') or '')[:4]})
        count=data.get('release-group-count')
        if len(groups)<100 or (isinstance(count,int) and offset+len(groups)>=count):break
    return found[:max_groups]


async def expanded_external_artist_detail(request,auth,ext_id):
    """Return albums for an external artist fetched on page open.

    Public MusicBrainz albums plus user-scoped DN albums are merged by stable
    album identity. If providers are down, cached albums are still returned.
    """
    if not ext_id.startswith('cassette-request-artist_'):
        return external_detail(ext_id,'getArtist')
    with connect() as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM discovery_artists WHERE id=?',(ext_id,)).fetchone()
    if row is None:return external_detail(ext_id,'getArtist')
    artist=dict(row)
    name=artist['name']
    client=request.app.state.client

    # Query the authenticated family member's DN catalog; this also helps find
    # the correct MB artist identifier if it wasn't present on the search card.
    dn_catalog,dn_albums=await asyncio.gather(
        dn_json(client,auth['u'],'catalog',query=name),
        dn_json(client,auth['u'],'albums',query=name))
    artist_mbid=artist.get('mbid') or ''
    if not MBID.fullmatch(str(artist_mbid)):
        possibles=set()
        for a in (dn_catalog or {}).get('artists',[]) if isinstance(dn_catalog,dict) else []:
            if not isinstance(a,dict):continue
            a_name=a.get('title') or a.get('name')
            a_id=a.get('musicbrainz_id')
            if normalized(a_name)==normalized(name) and MBID.fullmatch(str(a_id or '')):
                possibles.add(a_id.lower())
        if len(possibles)==1:artist_mbid=next(iter(possibles))
    artist_mbid=await _resolve_artist_mbid(client,name,artist_mbid)
    if artist_mbid and artist_mbid != artist.get('mbid'):
        # Persist MB identity on the SAME artist id currently opened in Cassette.
        with connect() as db:
            db.execute('UPDATE discovery_artists SET mbid=? WHERE id=?',
                       (artist_mbid,ext_id))

    mb_groups=await _browse_artist_album_groups(client,artist_mbid)
    album_candidates=[]
    for data in (dn_catalog,dn_albums):
        if not isinstance(data,dict):continue
        for a in list(data.get('albums') or [])+list(data.get('results') or []):
            if not isinstance(a,dict):continue
            a_name=a.get('title') or a.get('name')
            a_artist=a.get('artist') or a.get('artist_name')
            if not a_name or normalized(a_artist)!=normalized(name):continue
            album_candidates.append(a)
    # DN provides track counts for some releases; MB supplies the complete
    # discography including records that are absent from DN's query index.
    for item in album_candidates:
        remember_remote_album(item.get('title') or item.get('name'),name,
            mbid=item.get('musicbrainz_id') or '',
            artist_mbid=artist_mbid,
            year=item.get('year'),song_count=item.get('track_count') or 0)
    for item in mb_groups:
        remember_remote_album(item['title'],name,mbid=item['musicbrainz_id'],
            artist_mbid=artist_mbid,year=item['year'])
    with connect() as db:
        db.row_factory=sqlite3.Row
        # Prior searches sometimes created albums using a name-derived artist
        # ID. Include those too, but never albums of a different artist.
        albums=[dict(a) for a in db.execute('SELECT * FROM discovery_albums')
                if (a['artist_id']==ext_id or normalized(a['artist'])==normalized(name))
                and (not mb_groups or str(a['mbid'] or '') in
                     {g['musicbrainz_id'] for g in mb_groups})]
    # Exact normalized artist+album dedup, preserving stable album IDs.
    unique={}
    for item in albums:
        key=(normalized(item['artist']),normalized(item['name']))
        if key not in unique or (not unique[key]['cover_id'] and item['cover_id']):
            unique[key]=item
    albums=list(unique.values())
    albums.sort(key=lambda a: (int(a['year'] or 0),normalized(a['name'])),reverse=True)
    listed=[]
    for a in albums[:1000]:
        album={'id':a['id'],'name':a['name'],'artist':a['artist'],
               'artistId':ext_id,'songCount':int(a['song_count'] or 0),
               'duration':0}
        if a['cover_id']:album['coverArt']=a['cover_id']
        if a['year']:album['year']=int(a['year'])
        if MBID.fullmatch(str(a['mbid'] or '')):album['musicBrainzId']=a['mbid']
        listed.append(album)
    result={'id':ext_id,'name':name,'albumCount':len(listed),'album':listed}
    if artist.get('cover_id'):result['coverArt']=artist['cover_id']
    if artist_mbid:result['musicBrainzId']=artist_mbid
    response=JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1',
        'artist':result}},headers={'Cache-Control':'private, no-store'})
    from artist_enrichment_v5 import merged_external_artist_page
    return await merged_external_artist_page(__import__('discovery'),request,auth,ext_id,response)


def unavailable_response():
    return error_response('This recording is not downloaded yet. Star it to request through DroppedNeedle.',70)


def external_detail(ext_id,kind,username=None):
    if kind=='getSong':
        with connect() as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM discovery WHERE id=?',(ext_id,)).fetchone()
        if not row:return error_response('External song not found')
        song=external_song(dict(row))
        if username:
            from saved_favorites import mark_songs
            mark_songs(username,[song])
        return JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1',
            'song':song}},headers={'Cache-Control':'no-store'})
    if kind=='getAlbum' and ext_id.startswith('cassette-request-album_'):
        with connect() as db:
            db.row_factory=sqlite3.Row
            album=db.execute('SELECT * FROM discovery_albums WHERE id=?',(ext_id,)).fetchone()
            songs=[external_song(dict(r)) for r in db.execute('SELECT * FROM discovery ORDER BY title')
                if external_album_id(r['artist'],r['album'])==ext_id]
        if not album and not songs:return error_response('External album not found')
        if username:
            from saved_favorites import mark_songs
            mark_songs(username,songs)
        item={'id':ext_id,'name':album['name'] if album else songs[0]['album'],
            'artist':album['artist'] if album else songs[0]['artist'],
            'songCount':len(songs) or (int(album['song_count']) if album else 0),
            'duration':sum(song.get('duration',0) for song in songs),'song':songs}
        if album and album['cover_id']:item['coverArt']=album['cover_id']
        if album and album['artist_id']:item['artistId']=album['artist_id']
        if username and album and album['mbid']:
            from album_requests_v6 import album_starred
            when=album_starred(username,album['mbid'])
            if when:item['starred']=when
        return JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1',
            'album':item}},headers={'Cache-Control':'no-store'})
    if kind=='getArtist' and ext_id.startswith('cassette-request-artist_'):
        with connect() as db:
            db.row_factory=sqlite3.Row
            artist=db.execute('SELECT * FROM discovery_artists WHERE id=?',(ext_id,)).fetchone()
            albums=list(db.execute('SELECT * FROM discovery_albums WHERE artist_id=? ORDER BY name',(ext_id,)))
        if not artist:return error_response('External artist not found')
        listed=[{'id':a['id'],'name':a['name'],'artist':a['artist'],'artistId':ext_id,
                 'songCount':int(a['song_count']),'duration':0,
                 **({'coverArt':a['cover_id']} if a['cover_id'] else {})} for a in albums]
        item={'id':ext_id,'name':artist['name'],'albumCount':max(1,len(listed)),'album':listed}
        if artist['cover_id']:item['coverArt']=artist['cover_id']
        return JSONResponse({'subsonic-response':{'status':'ok','version':'1.16.1',
            'artist':item}},headers={'Cache-Control':'no-store'})
    return error_response('External media ID is unsupported')


def external_album_id(artist,album):
    return 'cassette-request-album_'+str(uuid.uuid5(uuid.NAMESPACE_URL,'album:'+normalized(artist)+'|'+normalized(album)))


# CASSETTE_SILENT_PLACEHOLDER_V7: cached external songs are REAL identities
# with VIRTUAL streams. A placeholder never enters Navidrome's real library.
def _virtual_artist_link(item):
    artist=str(item.get('artist') or '')
    album=str(item.get('album') or '')
    if not artist:return ''
    with connect() as db:
        db.row_factory=sqlite3.Row
        if album:
            row=db.execute('SELECT artist_id FROM discovery_albums WHERE id=?',
                (external_album_id(artist,album),)).fetchone()
            if row is not None and row['artist_id']:
                return row['artist_id']
        rows=db.execute('SELECT id,name,mbid FROM discovery_artists').fetchall()
    matches=[x for x in rows if normalized(x['name'])==normalized(artist)]
    verified={x['id'] for x in matches if MBID.fullmatch(str(x['mbid'] or ''))}
    if len(verified)==1:return next(iter(verified))
    if len(matches)==1:return matches[0]['id']
    # Create the navigation target; never invent a MusicBrainz identifier.
    row=remember_remote_artist(artist)
    return row['id'] if row else ''


def external_song(item):
    result={'id':item['id'],'title':item['title'],'artist':item['artist'],
            'album':item['album'] or 'Requested Music',
            'albumId':external_album_id(item['artist'],item['album']),
            'duration':551,'track':1,'discNumber':1,
            'isDir':False,'isVideo':False,'type':'music','contentType':'audio/mpeg','suffix':'mp3',
            'path':'UNAVAILABLE - 9:11 silent placeholder (not in library)',
            'created':'2000-01-01T00:00:00Z'}
    artist_id=_virtual_artist_link(item)
    if artist_id:result['artistId']=artist_id
    recording=item.get('recording_mbid')
    if MBID.fullmatch(str(recording or '')):
        result['musicBrainzId']=recording
    if item.get('cover_id'):
        result['coverArt']=item['cover_id']
    return result
