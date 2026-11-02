"""Authenticated Sona catalogue over the household's existing acquisition pipeline.

Metadata is cached separately from availability: only Navidrome can say playable.
The client submits catalogue IDs, never download URLs or another user's identity.
"""
import asyncio
import time
from collections import OrderedDict
from typing import Literal
import httpx
from fastapi import Depends, Query
from pydantic import BaseModel, Field
import app as core
import edition_matching as editions
import album_request_bridge_v6 as albums

_cache = OrderedDict()
_inflight = {}
from weakref import WeakValueDictionary
_request_locks = WeakValueDictionary()


def valid_id(value):
    if not editions.MBID.fullmatch(value):
        core.err('Invalid catalogue ID', 400)
    return value.lower()


def credit(row):
    return ''.join(str(c.get('name') or (c.get('artist') or {}).get('name') or '') + str(c.get('joinphrase') or '')
                   for c in row.get('artist-credit', []) if isinstance(c, dict))


def artwork(group):
    return 'https://coverartarchive.org/release-group/' + group + '/front-500' if group else ''


async def metadata(user, path, params=None):
    token = core.linked_token(core.connection, core.cipher, user['id'])
    if not token:
        core.err('Connect your request account in the Requests tab to search Sona Music.', 409)
    # User-scoped provider responses can include requested/in-library flags.
    key = (user['id'], core.digest(token), path, tuple(sorted((params or {}).items())))
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < 600:
        _cache.move_to_end(key)
        return entry[1]
    async def fetch():
        try:
            async with httpx.AsyncClient(timeout=18, trust_env=False, follow_redirects=False) as client:
                response = await client.get(core.DN + path, params=params, headers={'Authorization': 'Bearer ' + token})
                if response.status_code in (401, 403):
                    core.err('Reconnect your request account in the Requests tab.', 409)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError('Invalid catalogue response')
        except (httpx.HTTPError, ValueError) as exc:
            core.LOG.warning('Sona catalogue unavailable: %s', type(exc).__name__)
            core.err('The music catalogue is temporarily unavailable. Try again.', 502)
        _cache[key] = (time.monotonic(), result)
        while len(_cache) > 256:
            _cache.popitem(last=False)
        return result
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(fetch())
        _inflight[key] = task
        def finished(done):
            _inflight.pop(key, None)
            if not done.cancelled(): done.exception()
        task.add_done_callback(finished)
    return await asyncio.shield(task)


def album_item(row):
    group = str(row.get('musicbrainz_id') or row.get('id') or '')
    return {'id': group, 'kind': 'album', 'title': row.get('title', ''),
            'artist': row.get('artist') or credit(row), 'year': row.get('year'), 'cover': artwork(group)}


async def base_search(user, query):
    return await metadata(user, '/api/v1/search', {'q': query.strip(), 'limit_artists': 20, 'limit_albums': 35})


def exact_artist(data, query):
    candidates = [data.get('top_artist')] + data.get('artists', [])
    return next((row for row in candidates if row and core.normalize(row.get('title')) == core.normalize(query)
                 and editions.MBID.fullmatch(str(row.get('musicbrainz_id', '')))), None)


@core.app.get('/v1/sona/search')
async def search(q: str = Query(min_length=2, max_length=160), user=Depends(core.require_user)):
    data = await base_search(user, q)
    artist = exact_artist(data, q)
    album_rows = [album_item(row) for row in data.get('albums', []) if editions.MBID.fullmatch(str(row.get('musicbrainz_id', '')))]
    if artist:
        releases = await metadata(user, '/api/v1/artists/' + artist['musicbrainz_id'] + '/releases', {'limit': 50})
        own = [album_item({**row, 'artist': artist['title']}) for kind in ('albums','eps','singles') for row in releases.get(kind, [])
               if editions.MBID.fullmatch(str(row.get('id', '')))]
        seen = {row['id'] for row in own}
        album_rows = own + [row for row in album_rows if row['id'] not in seen]
    return {'albums': album_rows,
            'artists': [{'id': row['musicbrainz_id'], 'kind': 'artist', 'title': row['title'], 'artist': row['title']}
                        for row in data.get('artists', []) if editions.MBID.fullmatch(str(row.get('musicbrainz_id', '')))],
            'status': data.get('bucket_status') or {}}


@core.app.get('/v1/sona/songs')
async def songs(q: str = Query(min_length=2, max_length=160), user=Depends(core.require_user)):
    # Recording search is independent of album search, so a slow provider never
    # holds back local songs or the album/artist results. Uses the durable shared
    # MusicBrainz cache, request coalescing and global provider rate limit.
    if not core.linked_token(core.connection, core.cipher, user['id']):
        core.err('Connect your request account in the Requests tab.', 409)
    artists = await base_search(user, q)
    artist = exact_artist(artists, q)
    if artist:
        popular = await metadata(user, '/api/v1/artists/' + artist['musicbrainz_id'] + '/top-songs', {'count': 40})
        top = [{'id': row['recording_mbid'], 'kind': 'song', 'title': row['title'], 'artist': row['artist_name'],
                'album': row.get('release_name') or '', 'albumId': row.get('release_group_mbid') or '',
                'cover': artwork(row.get('release_group_mbid'))}
               for row in popular.get('songs', []) if editions.MBID.fullmatch(str(row.get('recording_mbid') or ''))]
        if top: return {'songs': top}
    terms = core.normalize(q).split()
    escaped = 'arid:' + artist['musicbrainz_id'] if artist else ' AND '.join('(recording:"' + word + '" OR artist:"' + word + '")' for word in terms)
    data = await editions.mb_get('recording', {'query': escaped, 'limit': 40})
    rows = []
    for row in data.get('recordings', []):
        if not editions.MBID.fullmatch(str(row.get('id', ''))): continue
        releases = [r for r in row.get('releases', []) if r.get('status') == 'Official']
        release = next((r for r in releases if (r.get('release-group') or {}).get('primary-type') == 'Album'), next(iter(releases), {}))
        group = (release.get('release-group') or {}).get('id', '')
        artist = credit(row)
        if not artist or not row.get('title'): continue
        rows.append({'id': row['id'], 'kind': 'song', 'title': row['title'], 'artist': artist,
                     'album': release.get('title', ''), 'albumId': group,
                     'duration': (row.get('length') or 0) / 1000, 'cover': artwork(group)})
    return {'songs': rows}


@core.app.get('/v1/sona/artists/{artist_id}')
async def artist(artist_id: str, user=Depends(core.require_user)):
    artist_id = valid_id(artist_id)
    data = await editions.mb_get('release-group', {'artist': artist_id, 'inc': 'artist-credits', 'limit': 100})
    rows = data.get('release-groups') or []
    return {'albums': [album_item(row) for row in rows], 'title': credit(rows[0]) if rows else 'Artist'}


async def album_metadata(user, album_id):
    basic, tracks = await asyncio.gather(metadata(user, '/api/v1/albums/' + album_id + '/basic'),
                                         metadata(user, '/api/v1/albums/' + album_id + '/tracks'))
    return {**basic, **tracks}


def match_song(track, local, artist, album):
    matches = [song for song in local if str(song.get('musicBrainzId') or '').lower() == str(track.get('recording_id') or '').lower() and track.get('recording_id')]
    if not matches:
        matches = [song for song in local if core.normalize(song.get('title')) == core.normalize(track.get('title'))
                   and core.normalize(song.get('artist')) == core.normalize(artist)
                   and core.normalize(song.get('album')) == core.normalize(album)
                   and (not song.get('musicBrainzId') or not track.get('recording_id'))]
    return matches[0] if len(matches) == 1 else None


@core.app.get('/v1/sona/albums/{album_id}')
async def album(album_id: str, user=Depends(core.require_user)):
    album_id = valid_id(album_id)
    data = await album_metadata(user, album_id)
    title, artist_name = data.get('title', ''), data.get('artist_name', '')
    local_ids = await albums.find_album_tracks(user, artist_name, title, album_id)
    local_album = None
    if local_ids:
        song = (await core.sub(user, 'getSong', {'id': local_ids[0]})).get('song') or {}
        if song.get('albumId'):
            local_album = (await core.sub(user, 'getAlbum', {'id': song['albumId']})).get('album')
    local = (local_album or {}).get('song') or []
    tracks = []
    for index, track in enumerate(data.get('tracks') or []):
        recording = track.get('recording_id') or ''
        tracks.append({'id': recording or f'{album_id}:{index}', 'kind': 'song', 'title': track.get('title', ''),
                       'artist': artist_name, 'album': title, 'albumId': album_id,
                       'track': track.get('position', index + 1), 'disc': track.get('disc_number', 1),
                       'duration': (track.get('length') or 0) / 1000, 'cover': artwork(album_id),
                       'requestable': bool(editions.MBID.fullmatch(recording)),
                       'localSong': match_song(track, local, artist_name, title)})
    complete = bool(tracks) and all(t['localSong'] for t in tracks)
    return {'id': album_id, 'kind': 'album', 'title': title, 'artist': artist_name, 'year': data.get('year'),
            'cover': artwork(album_id), 'tracks': tracks, 'localAlbum': local_album if complete else None}


class CatalogueRequest(BaseModel):
    kind: Literal['song', 'album']
    id: str = Field(min_length=36, max_length=36)
    album_id: str = Field(default='', max_length=36)


@core.app.post('/v1/sona/request')
async def request(body: CatalogueRequest, user=Depends(core.require_user)):
    item_id = valid_id(body.id)
    if body.album_id: valid_id(body.album_id)
    key = (user['id'], body.kind, item_id)
    lock = _request_locks.setdefault(key, asyncio.Lock())
    # Core ledger is durable and idempotent. This also coalesces simultaneous taps.
    async with lock:
        if body.kind == 'album':
            data = await album_metadata(user, item_id)
            count = len(data.get('tracks') or [])
            if not 1 <= count <= 1000: core.err('Album track list could not be verified.', 422)
            result = await albums.request_album(albums.AlbumRequest(username=user['username'], artist=data['artist_name'], album=data['title'], release_group_mbid=item_id, expected_tracks=count))
        else:
            if body.album_id:
                data = await album_metadata(user, body.album_id)
                track = next((row for row in data.get('tracks', []) if row.get('recording_id') == item_id), None)
                if not track: core.err('This recording is not on this album.', 422)
                artist_name, title, album_name = data['artist_name'], track['title'], data['title']
            else:
                data = await editions.mb_get('recording/' + item_id, {'inc': 'artist-credits'})
                artist_name, title, album_name = credit(data), data['title'], ''
            result = await core.manual_request(core.ManualRequestIn(kind='song', artist=artist_name, title=title, album=album_name, recording_mbid=item_id), user=user)
        core.emit_live_request_status(user['id'], body.kind if body.kind == 'album' else 'track', item_id, result.get('status', 'requested'), result.get('message', ''))
        return result
