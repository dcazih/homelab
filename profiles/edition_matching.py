"""Cassette edition resolver. Adapted from the existing Muse automation.py matcher.

Uses the same exact album/release/recording rules and shared MusicBrainz
metadata cache. No DroppedNeedle submission happens in this module.
"""
import asyncio
import datetime
import email.utils
import hashlib
import json
import logging
import os
import re
import time

import httpx
from fastapi import HTTPException
import app as core

LOG = logging.getLogger('music_profiles.edition_matching')
MBID = re.compile(r'^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$')
MB_INTERVAL = 1.2
MB_CACHE_TTL = 7 * 86400
MB_STALE_TTL = 30 * 86400
MB_MAX_ATTEMPTS = 3
MB_EDITION_LOOKUPS = 6
MB_RECORDING_CANDIDATES = 3
_MB_INFLIGHT_LOCK = asyncio.Lock()
_MB_INFLIGHT = {}

def _mb_prepare_tables():
    # Shared SQLite database persists across restarts and coordinates multiple workers.
    with core.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS mb_metadata_cache (
            cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS mb_request_budget (
            id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL NOT NULL)""")
        db.execute('INSERT OR IGNORE INTO mb_request_budget(id,next_at) VALUES(1,0)')


_mb_prepare_tables()


def _mb_cache_key(path,params):
    return hashlib.sha256(json.dumps([path,params or {}],sort_keys=True,
                                    separators=(',',':')).encode()).hexdigest()


def _mb_cached(key,allow_stale=False):
    with core.connection() as db:
        row=db.execute('SELECT payload,fetched_at FROM mb_metadata_cache WHERE cache_key=?',
                       (key,)).fetchone()
    if row and time.time()-row['fetched_at'] <= (MB_STALE_TTL if allow_stale else MB_CACHE_TTL):
        try:return json.loads(row['payload'])
        except (ValueError,TypeError):pass
    return None


def _mb_save(key,data):
    with core.connection() as db:
        db.execute('INSERT OR REPLACE INTO mb_metadata_cache(cache_key,payload,fetched_at) VALUES(?,?,?)',
                   (key,json.dumps(data),time.time()))


def _mb_reserve_slot():
    # BEGIN IMMEDIATE serializes reservations across Uvicorn processes. The lock
    # is held only for a tiny DB transaction, NEVER during sleep or network I/O.
    with core.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        now=time.time()
        row=db.execute('SELECT next_at FROM mb_request_budget WHERE id=1').fetchone()
        slot=max(now,float(row['next_at']))
        db.execute('UPDATE mb_request_budget SET next_at=? WHERE id=1',
                   (slot+MB_INTERVAL,))
    return max(0,slot-time.time())


def _mb_pause_all(delay):
    # Honor a provider cooldown globally, including other Bridge workers.
    with core.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE mb_request_budget SET next_at=max(next_at,?) WHERE id=1',
                   (time.time()+delay,))


def _mb_retry_after(header):
    if not header:return None
    try:return max(0,min(120,float(header)))
    except (ValueError,TypeError):pass
    try:
        dt=email.utils.parsedate_to_datetime(header)
        return max(0,min(120,(dt-datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
    except (ValueError,TypeError,OverflowError):return None


async def _mb_fetch(path,params,key):
    headers={'User-Agent':os.environ.get('BRIDGE_MUSICBRAINZ_USER_AGENT',
                                      'FamilyMusicBridge/2.2 (self-hosted personal music library)')}
    last_error=None
    for attempt in range(MB_MAX_ATTEMPTS):
        await asyncio.sleep(_mb_reserve_slot())
        try:
            async with httpx.AsyncClient(timeout=18,follow_redirects=False) as client:
                response=await client.get('https://musicbrainz.org/ws/2/'+path,
                                          params={**(params or {}),'fmt':'json'},headers=headers)
            if response.status_code in (429,503,502,504):
                last_error=HTTPException(503,'MusicBrainz temporarily unavailable or rate limiting')
                if attempt+1 < MB_MAX_ATTEMPTS:
                    delay=max(min(8,2**(attempt+1)),_mb_retry_after(response.headers.get('Retry-After')) or 0)
                    _mb_pause_all(delay)
                    continue
                break
            response.raise_for_status()  # 4xx besides 429 must not be retried
            data=response.json()
            if not isinstance(data,dict):raise ValueError('Unexpected MusicBrainz JSON')
            _mb_save(key,data)
            return data
        except (httpx.TimeoutException,httpx.TransportError) as exc:
            last_error=HTTPException(503,'MusicBrainz connection temporarily unavailable')
            if attempt+1 < MB_MAX_ATTEMPTS:
                _mb_pause_all(min(8,2**(attempt+1)))
                continue
        except (httpx.HTTPStatusError,ValueError) as exc:
            last_error=HTTPException(502,'MusicBrainz metadata lookup failed')
            break
    cached=_mb_cached(key,allow_stale=True)
    if cached is not None:
        LOG.warning('Using stale MusicBrainz metadata after provider failure: %s',path)
        return cached
    raise last_error or HTTPException(503,'MusicBrainz metadata unavailable')


async def mb_get(path,params=None):
    """Cached, cross-worker rate-limited MusicBrainz reads with coalesced calls.

    No background polling, no duplicate request submission, no failed-response caching.
    """
    key=_mb_cache_key(path,params)
    cached=_mb_cached(key)
    if cached is not None:return cached
    async with _MB_INFLIGHT_LOCK:
        task=_MB_INFLIGHT.get(key)
        if task is None or task.done():
            task=asyncio.create_task(_mb_fetch(path,params,key))
            _MB_INFLIGHT[key]=task
            task.add_done_callback(lambda t,k=key: _MB_INFLIGHT.pop(k,None)
                                   if _MB_INFLIGHT.get(k) is t else None)
    return await asyncio.shield(task)


def mb_artist_matches(credit,artist):
    if not isinstance(credit,list):return False
    return any(isinstance(a,dict) and core.normalize(a.get('artist',{}).get('name'))==core.normalize(artist)
               for a in credit)


def mb_original_release_candidates(groups,artist,album):
    """Choose only an exact official artist/album release group, not remix/live editions."""
    matches=[]
    for group in groups:
        if not isinstance(group,dict):continue
        if not MBID.fullmatch(str(group.get('id',''))):continue
        if core.normalize(group.get('title'))!=core.normalize(album):continue
        if not mb_artist_matches(group.get('artist-credit'),artist):continue
        if group.get('primary-type')!='Album':continue
        secondary=set(group.get('secondary-types') or [])
        if secondary & {'Remix','Live','Compilation','DJ-mix','Mixtape/Street','Soundtrack'}:continue
        matches.append(group)
    # A genuine distinct album with the same title should not be silently selected.
    return matches


def mb_release_rank(release):
    """Prefer original official album editions, not later reissues or special versions."""
    name=(release.get('title') or '').casefold()
    extra=bool(re.search(r'\b(deluxe|expanded|remaster|anniversary|bonus|instrumental|clean|radio|live|remix|edition)\b',name))
    mediums=release.get('media') or []
    formats=[(m.get('format') or '').casefold() for m in mediums if isinstance(m,dict)]
    preferred=any('digital media' in f or f=='cd' for f in formats)
    # Date ISO sorts correctly; missing dates rank last.
    date=release.get('date') or '9999'
    return (extra,not preferred,date,release.get('id',''))


async def dn_resolve_album_track(user,artist,title,album):
    """Find exact track recordings on official original-album editions.

    Each candidate pairs a recording ID with its exact MusicBrainz release ID.
    DroppedNeedle accepts release_id in TrackRequestBody, so the selected edition
    must be pinned alongside its recording. Never mix identities across editions.
    """
    safe=lambda s:s.replace('"','').strip()
    data=await mb_get('release-group',{'query':f'releasegroup:"{safe(album)}" AND artist:"{safe(artist)}"',
                                        'limit':25})
    groups=mb_original_release_candidates(data.get('release-groups',[]),artist,album)
    if len(groups)!=1:
        return []  # no album match, or genuinely different groups; do not guess
    group=groups[0]
    data=await mb_get('release',{'release-group':group['id'],'limit':100})
    releases=[r for r in data.get('releases',[]) if isinstance(r,dict)
              and MBID.fullmatch(str(r.get('id','')))
              and core.normalize(r.get('title'))==core.normalize(album)
              and r.get('status')=='Official']
    releases.sort(key=mb_release_rank)
    candidates=[]
    seen=set()
    for edition in releases[:MB_EDITION_LOOKUPS]:
        details=await mb_get('release/'+edition['id'],{'inc':'recordings+artist-credits'})
        for medium in details.get('media',[]):
            for track in medium.get('tracks',[]):
                recording=track.get('recording') or {}
                mbid=recording.get('id')
                if not MBID.fullmatch(str(mbid or '')):continue
                if core.normalize(track.get('title'))!=core.normalize(title):continue
                if core.normalize(recording.get('title'))!=core.normalize(title):continue
                credit=recording.get('artist-credit') or track.get('artist-credit')
                if credit and not mb_artist_matches(credit,artist):continue
                pair=(mbid,edition['id'])
                if pair not in seen:
                    seen.add(pair)
                    candidates.append(pair)
                if len(candidates)>=MB_RECORDING_CANDIDATES:return candidates
    return candidates


async def dn_resolve_track(user,artist,title):
    """Fallback only when the request did not specify an album."""
    safe=lambda s:s.replace('"','').strip()
    data=await mb_get('recording',{'query':f'recording:"{safe(title)}" AND artist:"{safe(artist)}"',
                                   'limit':25})
    matches=[]
    for item in data.get('recordings',[]):
        if (core.normalize(item.get('title'))==core.normalize(title)
            and mb_artist_matches(item.get('artist-credit'),artist)
            and MBID.fullmatch(str(item.get('id','')))
            and int(item.get('score',0))>=95):
            matches.append(item['id'])
    return sorted(set(matches))


SINGLE_FROM_RELATION_ID = 'fcf680a9-6871-4519-8c4b-8c6549575b35'

async def resolve_cassette_album_track(user,artist,title,album):
    """Resolve a verified recording/release pair, even when search labels an album single.

    A title-matched single may be linked by MusicBrainz to its original studio
    album. Follow that unique explicit link, validate the album artist/type,
    then use the existing exact edition resolver. Never infer an album from
    matching titles alone and never submit an unverified recording/release pair.

    Returns (canonical_album_title, [(recording_mbid, release_mbid), ...]).
    """
    matches=await dn_resolve_album_track(user,artist,title,album)
    if matches:
        return album,matches
    # This fallback is intentionally narrow: only a song shown as its own
    # single, e.g. 'N95' (album='N95'), is automatically redirected.
    if not album or core.normalize(album)!=core.normalize(title):
        return album,[]
    safe=lambda s:s.replace('"','').strip()
    data=await mb_get('release-group',{
        'query':f'releasegroup:"{safe(album)}" AND artist:"{safe(artist)}"',
        'limit':25})
    singles=[g for g in data.get('release-groups',[])
             if isinstance(g,dict)
             and MBID.fullmatch(str(g.get('id','')))
             and g.get('primary-type') in ('Single','EP')
             and core.normalize(g.get('title'))==core.normalize(album)
             and mb_artist_matches(g.get('artist-credit'),artist)
             and not (set(g.get('secondary-types') or []) &
                      {'Remix','Live','Compilation','DJ-mix','Mixtape/Street','Soundtrack'})]
    if len(singles)!=1:
        return album,[]
    details=await mb_get('release-group/'+singles[0]['id'],
                         {'inc':'release-group-rels'})
    target_ids=set()
    for relation in details.get('relations',[]):
        if not isinstance(relation,dict):continue
        if relation.get('type-id')!=SINGLE_FROM_RELATION_ID:continue
        if relation.get('direction')!='forward':continue
        # MusicBrainz JSON uses `release_group` (underscore) for the
        # relationship target. Some clients expose `release-group` instead.
        # The old code only checked the hyphenated spelling, silently
        # discarding valid `single from` links.
        if relation.get('target-type') not in (None, 'release_group', 'release-group'):
            continue
        target = relation.get('release_group') or relation.get('release-group') or {}
        if isinstance(target, dict):
            target_id = target.get('id')
        else:
            target_id = None
        # Some MB clients provide the UUID directly under `target`.
        if not target_id:
            raw_target = relation.get('target')
            target_id = raw_target.get('id') if isinstance(raw_target, dict) else raw_target
        if not target_id:
            target_id = relation.get('target-id') or relation.get('target_id')
        if MBID.fullmatch(str(target_id or '')):
            target_ids.add(str(target_id))
    # Different linked albums require selection, not a guessed edition.
    if len(target_ids)!=1:
        return album,[]
    linked=await mb_get('release-group/'+next(iter(target_ids)),
                        {'inc':'artist-credits'})
    if (linked.get('primary-type')!='Album'
        or not mb_artist_matches(linked.get('artist-credit'),artist)
        or set(linked.get('secondary-types') or []) &
        {'Remix','Live','Compilation','DJ-mix','Mixtape/Street','Soundtrack'}):
        return album,[]
    canonical_title=linked.get('title') or ''
    if not canonical_title or core.normalize(canonical_title)==core.normalize(album):
        return album,[]
    matches=await dn_resolve_album_track(user,artist,title,canonical_title)
    if not matches:
        return album,[]
    LOG.info('Resolved single metadata through verified original-album relation')
    return canonical_title,matches
