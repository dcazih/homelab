"""Verified artist identity, consistent result cards and merged artist discographies.

No remote submission, mutations, or credentials persisted by this module.
"""
import asyncio
import logging
import os
import re
from copy import deepcopy

LOG=logging.getLogger('cassette.artist_v5')


def _words(value):
    from music_search_upgrade import norm
    return norm(value)


def _similar(query, candidate):
    """Max two edits for multi-word names, with high confidence requirement."""
    from music_search_upgrade import typo_distance
    q=_words(query);n=_words(candidate)
    if not q or not n:return None
    if q==n:return 0
    cost=typo_distance(q,n)
    if cost is None:return None
    # Avoid guessing short ambiguous names such as 'Ye' versus 'Ye-A'.
    if len(q)<6 or cost>2 or (1-cost/max(len(q),len(n))) < .82:return None
    return cost


async def fuzzy_identity(d,client,query):
    """Find one close verified MusicBrainz name/alias, never a substring match."""
    from artist_identity import normalized_identity
    q=normalized_identity(query)
    tokens=[t for t in q.split() if len(t)>=3]
    if not tokens or len(q)<6:return None
    # MusicBrainz Lucene terms can be fuzzy; fall back to a single distinctive
    # term if the multiword query doesn't index a historical artist alias.
    clauses=[]
    if len(tokens)>1:
        for field in ('artist','alias'):
            clauses.append(' AND '.join(f'{field}:{re.sub(r"[^\w]", "", t)}~0.65' for t in tokens[:4]))
    clauses.append(f'artist:{tokens[0]}~0.65 OR alias:{tokens[0]}~0.65')
    rows={}
    for clause in clauses[:3]:
        try:
            response=await d._artist_mb_get(client,'artist/',{'query':clause,'limit':20})
        except Exception as exc:
            LOG.info('Fuzzy artist lookup unavailable: %s',type(exc).__name__)
            continue
        for item in response.get('artists',[]) if isinstance(response,dict) else []:
            if not isinstance(item,dict) or not d.MBID.fullmatch(str(item.get('id') or '')):continue
            rows[item['id']]=item
        if len(rows)>=10:break
    matched=[]
    detailed_lookups=0
    for candidate in list(rows.values())[:8]:
        names=[candidate.get('name') or '']
        names += [a.get('name','') for a in candidate.get('aliases',[]) if isinstance(a,dict)]
        costs=[_similar(q,n) for n in names]
        costs=[c for c in costs if c is not None]
        # MusicBrainz search results do not always include aliases. Load a
        # bounded number of artist details before discarding a near match.
        if not costs and detailed_lookups<4:
            detailed_lookups+=1
            try:
                details=await d._artist_mb_get(client,'artist/'+candidate['id'],{'inc':'aliases'})
                detail_names=[a.get('name','') for a in details.get('aliases',[])
                              if isinstance(a,dict)] if isinstance(details,dict) else []
                costs=[c for name in detail_names if (c:=_similar(q,name)) is not None]
            except Exception:
                pass
        if not costs:continue
        cost=min(costs)
        matched.append((cost,candidate))
    if not matched:return None
    matched.sort(key=lambda pair:(pair[0], -int(pair[1].get('score') or 0)))
    winning=matched[0]
    # A distinct artist at the same edit distance means ambiguity: show none.
    if len(matched)>1 and matched[1][0]==winning[0]:return None
    winner=winning[1]
    return {'id':winner['id'].lower(),'canonical':winner.get('name') or query,
            'display':winner.get('name') or query,
            'aliases':[a.get('name','') for a in winner.get('aliases',[]) if isinstance(a,dict)],
            'score':int(winner.get('score') or 0),'direct':False}


def credible_match(d,query,songs,artists,albums):
    """Ignore weak token/substring results when deciding whether to try correction."""
    q=d.normalized(query)
    if not q:return False
    for row in artists:
        if d.normalized(row.get('name'))==q:return True
    for row in albums:
        if d.normalized(row.get('name'))==q:return True
    for row in songs:
        if d.normalized(row.get('title'))==q:return True
    return False


def tidy_identity_card(d,result):
    if result is None:return None
    songs,artists,albums,identity=result
    if not albums:
        # No verified discography: don't render a dead-end artist card.
        return None
    with d.connect() as db:
        db.row_factory=d.sqlite3.Row
        row=db.execute('SELECT cover_id,album_count FROM discovery_artists WHERE id=?',
                       (artists[0]['id'],)).fetchone()
    card=dict(artists[0])
    card['albumCount']=max(len(albums),int(row['album_count'] or 0)) if row else len(albums)
    if row and row['cover_id']:card['coverArt']=row['cover_id']
    return songs,[card],albums,identity


async def _local_artist_albums(d,client,upstream,auth,identity):
    """Read owned albums from Navidrome's artist index, not string matches in titles."""
    from personal import nav_json
    wanted={d.normalized(identity.get('canonical')),d.normalized(identity.get('display'))}
    wanted.update(d.normalized(x) for x in identity.get('aliases',[]) if x)
    wanted.discard('')
    try:
        # getArtists provides native stable artist IDs for whole-library matching.
        payload=await asyncio.wait_for(nav_json(client,upstream,'getArtists',auth),timeout=10)
    except Exception as exc:
        LOG.info('Navidrome artist index unavailable: %s',type(exc).__name__)
        return []
    index=(payload.get('artists') or {}).get('index') or []
    matches=[]
    for bucket in index:
        for a in bucket.get('artist',[]) if isinstance(bucket,dict) else []:
            if not isinstance(a,dict):continue
            aid=a.get('id');name=a.get('name') or ''
            if not aid:continue
            if d.normalized(name) in wanted or (str(a.get('musicBrainzId') or '').lower()==identity.get('id')):
                matches.append(str(aid))
    if len(matches)>5:
        LOG.warning('More than five matching Navidrome artist IDs; refusing ambiguous merge')
        return []
    albums=[]
    for aid in dict.fromkeys(matches):
        try:
            data=await asyncio.wait_for(nav_json(client,upstream,'getArtist',auth,{'id':aid}),timeout=10)
            album_list=(data.get('artist') or {}).get('album') or []
            for a in album_list:
                if not isinstance(a,dict) or not a.get('id') or not a.get('name'):continue
                album=dict(a)
                album['artistId']=aid
                albums.append(album)
        except Exception as exc:
            LOG.info('Navidrome artist albums unavailable: %s',type(exc).__name__)
    return albums


def merge_albums(d,remote,local,artist_id):
    """Prefer playable local album IDs; retain remote cover when local art missing."""
    unique={}
    for item in remote:
        key=d.normalized(item.get('name'))
        if key:unique[key]=dict(item)
    for item in local:
        key=d.normalized(item.get('name'))
        if not key:continue
        saved=unique.get(key) or {}
        entry=dict(item)
        if not entry.get('coverArt') and saved.get('coverArt'):
            entry['coverArt']=saved['coverArt']
        if not entry.get('year') and saved.get('year'):
            entry['year']=saved['year']
        if not entry.get('musicBrainzId') and saved.get('musicBrainzId'):
            entry['musicBrainzId']=saved['musicBrainzId']
        remote_count=int(saved.get('songCount') or 0)
        local_count=int(entry.get('songCount') or 0)
        if saved and remote_count and local_count < remote_count:
            # Keep the complete external release's ID, not a partial native copy.
            full=dict(saved)
            if not full.get('coverArt') and entry.get('coverArt'):
                full['coverArt']=entry['coverArt']
            unique[key]=full
        else:
            unique[key]=entry
    result=list(unique.values())
    result.sort(key=lambda a:(int(a.get('year') or 0),d.normalized(a.get('name'))),reverse=True)
    return result


async def merged_external_artist_page(d,request,auth,ext_id,original_result):
    """Enhance existing external artist response with verified native albums."""
    from fastapi.responses import JSONResponse
    original=original_result.body
    import json
    data=json.loads(original)
    envelope=data.get('subsonic-response') or {}
    artist=envelope.get('artist') or {}
    mbid=artist.get('musicBrainzId') or ''
    if not d.MBID.fullmatch(str(mbid)):return original_result
    try:
        from artist_identity import resolve_exact_artist
        # Exact validated MBID is stronger than display name; avoid resolving
        # another artist from a prefix such as Ye.
        with d.connect() as db:
            db.row_factory=d.sqlite3.Row
            row=db.execute('SELECT name FROM discovery_artists WHERE id=?',(ext_id,)).fetchone()
        identity={'id':mbid.lower(),'canonical':row['name'] if row else artist.get('name'),
                  'display':artist.get('name'),'aliases':[]}
        details=await d._artist_mb_get(request.app.state.client,'artist/'+mbid,{'inc':'aliases'})
        if isinstance(details,dict):
            identity['canonical']=details.get('name') or identity['canonical']
            identity['aliases']=[a.get('name','') for a in details.get('aliases',[]) if isinstance(a,dict)]
        upstream=os.environ.get('NAVIDROME_UPSTREAM','http://host.docker.internal:4533')
        local=await _local_artist_albums(d,request.app.state.client,upstream,auth,identity)
        combined=merge_albums(d,artist.get('album') or [],local,ext_id)
        artist['album']=combined
        artist['albumCount']=len(combined)
        if not artist.get('coverArt'):
            artist['coverArt']=next((a.get('coverArt') for a in combined if a.get('coverArt')),None)
            if not artist.get('coverArt'):artist.pop('coverArt',None)
        return JSONResponse({'subsonic-response':{**envelope,'artist':artist}},headers={'Cache-Control':'private, no-store'})
    except Exception as exc:
        LOG.warning('Artist album merge skipped: %s',type(exc).__name__)
        return original_result


async def merged_native_artist_page(d,request,upstream,auth,artist_id):
    """Native artist page with additional out-of-library MB releases.

    Failure to resolve remote metadata returns original Navidrome response.
    """
    from personal import nav_json
    from fastapi.responses import JSONResponse
    from artist_identity import resolve_exact_artist
    native=await nav_json(request.app.state.client,upstream,'getArtist',auth,{'id':artist_id})
    artist=native.get('artist') or {}
    if not artist or not artist.get('name'):
        return JSONResponse({'subsonic-response':{**native}},headers={'Cache-Control':'private, no-store'})
    try:
        identity=await resolve_exact_artist(d,request.app.state.client,artist['name'])
        if identity is None:return JSONResponse({'subsonic-response':native},headers={'Cache-Control':'private, no-store'})
        groups=await d._browse_artist_album_groups(request.app.state.client,identity['id'],max_groups=1000)
        remote=[]
        for g in groups:
            album=d.remember_remote_album(g['title'],identity['canonical'],g['musicbrainz_id'],identity['id'],year=g.get('year'))
            if album:remote.append(album)
        combined=merge_albums(d,remote,artist.get('album') or [],artist_id)
        artist['album']=combined
        artist['albumCount']=len(combined)
        native['artist']=artist
    except Exception as exc:
        LOG.info('Native artist catalog enrichment unavailable: %s',type(exc).__name__)
    return JSONResponse({'subsonic-response':native},headers={'Cache-Control':'private, no-store'})
