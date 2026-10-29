"""Stage 3.1: personal library lists with globally complete detail pages.

Only list membership is filtered. All media identifiers are real Navidrome IDs,
so Cassette's album/artist details and local favorites remain globally consistent.
"""
import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qsl
import xml.etree.ElementTree as ET

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

LOG = logging.getLogger('cassette_personal')
PLAYLIST_NAME = os.getenv('CASSETTE_PERSONAL_PLAYLIST', 'My Music')


def personal_playlist_name(username):
    """Use the signed-in Navidrome username, never a supplied display name."""
    return f"{username}’s Music"


def is_personal_playlist(name, username):
    return name_matches(name, personal_playlist_name(username)) or name_matches(name, PLAYLIST_NAME)

CACHE_SECONDS = max(0.0, min(60.0, float(os.getenv('CASSETTE_PERSONAL_CACHE_SECONDS', '5'))))
MAX_PAGE_SIZE = 1000

class PersonalizationError(Exception):
    def __init__(self, message: str, code: int = 70):
        super().__init__(message)
        self.code = code


def to_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    raise PersonalizationError('Unexpected Navidrome response shape')


def error_response(message, code=70, fmt='json', status_code=200):
    """Return OpenSubsonic-shaped errors; no personal data and no HTTP 500 leaks."""
    if fmt == 'xml':
        elem = ET.Element('subsonic-response', {'xmlns':'http://subsonic.org/restapi', 'status':'failed', 'version':'1.16.1'})
        ET.SubElement(elem, 'error', {'code': str(code), 'message': message})
        return Response(content=ET.tostring(elem, encoding='utf-8', xml_declaration=True),
                        media_type='application/xml', status_code=status_code,
                        headers={'Cache-Control':'no-store'})
    return JSONResponse({'subsonic-response': {'status':'failed', 'version':'1.16.1',
                        'type':'navidrome', 'error': {'code': code, 'message':message}}},
                        status_code=status_code, headers={'Cache-Control':'no-store'})


def base_envelope(payload_key, payload, source):
    # Preserve the server's headers when available. Do not return its auth/status error payload here.
    base = {k:v for k,v in source.items() if k not in ('error','artists','albumList2','searchResult3','playlists','playlist')}
    base.update({'status':'ok', 'version':source.get('version','1.16.1'), payload_key: payload})
    return {'subsonic-response':base}


def request_params(request: Request, body: bytes):
    pairs = list(request.query_params.multi_items())
    if body and request.method == 'POST':
        media_type = request.headers.get('content-type','').split(';')[0].strip().lower()
        if media_type != 'application/x-www-form-urlencoded':
            raise PersonalizationError('Personal library endpoints require standard Subsonic form parameters')
        try:
            pairs.extend(parse_qsl(body.decode('utf-8'), keep_blank_values=True, strict_parsing=False))
        except UnicodeDecodeError as exc:
            raise PersonalizationError('Invalid form encoding') from exc
    for key in ('u','t','s','p','apiKey','f'):
        if sum(1 for k,_ in pairs if k == key) > 1:
            raise PersonalizationError('Duplicate authentication or format parameter')
    return dict(pairs)


def credential_params(params):
    user = (params.get('u') or '').strip()
    if not user or (not (params.get('t') and params.get('s')) and not params.get('p')):
        # Can't identify the user from an API key alone without a trusted server identity API.
        raise PersonalizationError('Personal library requires Navidrome username/password authentication', 10)
    return {'u':user, **{k:params[k] for k in ('t','s','p','v','c') if k in params}, 'f':'json'}


def numeric(params, name, default, maximum=MAX_PAGE_SIZE):
    val = params.get(name, str(default))
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise PersonalizationError(f'Invalid {name}')
    if n < 0 or n > 2**31-1:
        raise PersonalizationError(f'Invalid {name}')
    return min(n, maximum) if name in ('size','songCount') else n


async def nav_json(client, upstream, endpoint, auth, extra=None):
    params = {**auth, **(extra or {})}
    # Our backend makes fixed-origin GET requests only. Never follow redirects to external hosts.
    try:
        response = await client.get(f'{upstream}/rest/{endpoint}.view', params=params,
                                    headers={'Accept':'application/json'}, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise PersonalizationError('Navidrome is unavailable or returned invalid JSON', 70) from exc
    root = data.get('subsonic-response',{})
    if not isinstance(root,dict):
        raise PersonalizationError('Invalid Navidrome response')
    if root.get('status') != 'ok':
        err = root.get('error') or {}
        # Preserve auth error codes without echoing backend secrets.
        if err.get('code') in (10, 40, 41):
            raise PersonalizationError('Navidrome authentication failed', int(err.get('code')))
        raise PersonalizationError('Navidrome rejected the request', 70)
    return root


def name_matches(value, target):
    return isinstance(value, str) and value.casefold() == target.casefold()


def album_id(song):
    return str(song.get('albumId') or '')


def artist_id(song):
    return str(song.get('artistId') or '')


async def personal_membership(request: Request, upstream, auth):
    """Always authenticate to Navidrome. Cache data only after verifying playlist ownership.

    cache key includes username + playlist id; cache is per-process and never persisted.
    """
    client = request.app.state.client
    user = auth['u']
    pls = await nav_json(client, upstream, 'getPlaylists', auth)
    playlists = to_list((pls.get('playlists') or {}).get('playlist'))
    mine = [p for p in playlists if is_personal_playlist(p.get('name'), user)
            and name_matches(p.get('owner'), user)]
    if len(mine) > 1:
        raise PersonalizationError('Two personal playlists exist; resolve the duplicate before modifying music')
    if not mine:
        return [], pls
    p = mine[0]
    if not name_matches(p.get('name'), personal_playlist_name(user)):
        await nav_json(client, upstream, 'updatePlaylist', auth, {
            'playlistId': str(p['id']), 'name': personal_playlist_name(user), 'public': 'false'})
    # Public playlists from your own account are accepted, but don't use someone else's.
    pid = str(p.get('id') or '')
    if not pid:
        raise PersonalizationError('Invalid My Music playlist identifier')
    key = (user.casefold(),pid)
    cache = request.app.state.personal_cache
    now = time.monotonic()
    record = cache.get(key)
    if record is not None and now - record[0] < CACHE_SECONDS:
        return record[1], pls
    detail = await nav_json(client, upstream, 'getPlaylist', auth, {'id':pid})
    playlist = detail.get('playlist') or {}
    if not name_matches(playlist.get('owner'),user) or str(playlist.get('id')) != pid:
        raise PersonalizationError('Playlist ownership could not be verified', 40)
    entries = to_list(playlist.get('entry'))
    seen = set()
    songs = []
    for song in entries:
        if not isinstance(song, dict) or not song.get('id'):
            continue
        sid = str(song['id'])
        if sid in seen:
            continue
        seen.add(sid)
        songs.append(song)
    cache[key] = (now, songs)
    # Prevent unbounded memory if lots of profiles connect over time.
    if len(cache) > 200:
        request.app.state.personal_cache = {key:(now,songs)}
    return songs, pls


def build_albums(songs):
    groups = defaultdict(list)
    for s in songs:
        if album_id(s):
            groups[album_id(s)].append(s)
    albums = []
    for aid, tracks in groups.items():
        first = tracks[0]
        obj = {'id':aid, 'name':first.get('album') or 'Unknown Album',
               'artist':first.get('albumArtist') or first.get('artist') or 'Unknown Artist',
               'songCount':len(tracks),
               'duration':sum(max(0,int(x.get('duration') or 0)) for x in tracks)}
        for field in ('artistId','coverArt','year','genre','created'):
            if first.get(field) is not None:
                obj[field] = first[field]
        albums.append(obj)
    return albums


def sort_albums(items, kind):
    if kind == 'alphabeticalByName':
        return sorted(items, key=lambda a:(a['name'].casefold(),a['artist'].casefold(),a['id']))
    if kind == 'newest':
        def key(a):
            return (str(a.get('created') or ''),str(a.get('year') or ''),a['id'])
        return sorted(items, key=key, reverse=True)
    return items


async def personalized_response(request, upstream, endpoint, body):
    try:
        params = request_params(request,body)
    except PersonalizationError as exc:
        return error_response(str(exc),exc.code)
    kind = endpoint[:-5] if endpoint.endswith('.view') else endpoint
    query = (params.get('query') or '').strip()
    # The real Search tab supplies a nonempty query. Let all such calls pass through untouched.
    if kind == 'search3' and query:
        return None
    # Discover's Recently Played / Most Played sections use Navidrome's
    # album rankings. Preserve those results and their ordering, but give each
    # card a personal-scoped album ID so opening it shows this user's saved
    # tracks only. Artist/search and other Discover album IDs remain native
    # and still open complete catalog editions.
    if kind == 'getAlbumList2' and params.get('type') in ('recent', 'frequent'):
        if (params.get('f') or 'xml').lower() != 'json':
            return None  # Do not attempt to rewrite XML responses.
        try:
            auth = credential_params(params)
            offset = numeric(params, 'offset', 0)
            count = numeric(params, 'size', 10)
            extra = {'type':params['type'], 'offset':str(offset), 'size':str(count)}
            if params.get('musicFolderId'):
                extra['musicFolderId'] = params['musicFolderId']
            root = await nav_json(request.app.state.client,upstream,'getAlbumList2',auth,extra)
            section = root.get('albumList2') or {}
            if not isinstance(section,dict):
                raise PersonalizationError('Invalid Navidrome album list')
            from virtual import as_virtual_album
            ranked = to_list(section.get('album'))
            section = {**section,'album':[as_virtual_album(a) for a in ranked
                       if isinstance(a,dict) and a.get('id')]}
            root = {**root,'albumList2':section}
            from cover_art import augment_payload
            augment_payload(root)
            return JSONResponse({'subsonic-response':root},headers={
                'Cache-Control':'private, no-store',
                'X-Cassette-Collection':'discover-personal-albums'})
        except PersonalizationError as exc:
            return error_response(str(exc),exc.code)
        except Exception:
            LOG.exception('Could not scope Discover album links')
            return error_response('Discover albums could not be loaded; retry later',70,status_code=502)
    # Other discovery album-list modes must retain their existing behavior.
    if kind == 'getAlbumList2' and params.get('type') not in ('alphabeticalByName','newest'):
        return None
    if kind not in ('search3','getArtists','getAlbumList2'):
        return None
    fmt = (params.get('f') or 'xml').lower()
    if fmt != 'json':
        return error_response('Personal library browsing currently requires f=json',70,fmt)
    if params.get('musicFolderId') or params.get('musicFolderIds'):
        return error_response('Selecting separate Navidrome music folders is not supported yet in Stage 2', 70)
    try:
        auth = credential_params(params)
        songs, meta = await personal_membership(request,upstream,auth)
        if kind == 'search3':
            offset = numeric(params, 'songOffset', 0)
            count = numeric(params, 'songCount', 20)
            # Cassette's Songs list sorts client-side; stable ordering for pagination.
            ordered = sorted(songs, key=lambda x:(str(x.get('title') or '').casefold(),str(x.get('artist') or '').casefold(),str(x['id'])))
            payload = {'artist':[], 'album':[], 'song':ordered[offset:offset+count]}
            result = base_envelope('searchResult3',payload,meta)
        elif kind == 'getAlbumList2':
            offset = numeric(params, 'offset', 0)
            count = numeric(params, 'size', 10)
            # Home collection albums use reversible virtual IDs. Their detail
            # pages can now be filtered without affecting artist/search albums.
            from virtual import as_virtual_album
            albums = sort_albums(build_albums(songs),'newest' if params['type'] == 'recent' else params['type'])
            albums = [as_virtual_album(a) for a in albums]
            result = base_envelope('albumList2',{'album':albums[offset:offset+count]},meta)
        else:
            base = await nav_json(request.app.state.client,upstream,'getArtists',auth)
            all_indices = to_list((base.get('artists') or {}).get('index'))
            # Song's artistId is authoritative. Avoid name matching across different artists.
            ids = {artist_id(s) for s in songs if artist_id(s)}
            result_indices = []
            for idx in all_indices:
                people = []
                for a in to_list(idx.get('artist')):
                    if str(a.get('id') or '') in ids:
                        # Retain original Navidrome IDs and total album counts.
                        # Opening the artist now shows the complete shared catalog.
                        people.append(dict(a))
                if people:
                    result_indices.append({**idx,'artist':people})
            result = base_envelope('artists', {'ignoredArticles':(base.get('artists') or {}).get('ignoredArticles',''),
                                                 'index':result_indices},base)
        from cover_art import augment_payload
        augment_payload(result['subsonic-response'])
        return JSONResponse(result,headers={'Cache-Control':'private, no-store',
                                            'X-Cassette-Collection':'personal'})
    except PersonalizationError as exc:
        return error_response(str(exc),exc.code)
    except Exception:
        LOG.exception('Personal catalog transformation failed (credentials never logged)')
        return error_response('Personal catalog could not be loaded; retry later',70,status_code=502)
