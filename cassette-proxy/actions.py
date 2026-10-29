"""Stage 3: song/album stars -> My Music synchronization, ADD ONLY.

Unstar remains an ordinary favorite removal in Navidrome and NEVER changes
My Music. We never infer intent from stream, download, scrobble or playback.
"""
import asyncio
import json
import logging
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi.responses import Response, JSONResponse
from personal import (
    PLAYLIST_NAME, personal_playlist_name, is_personal_playlist, PersonalizationError, credential_params,
    nav_json, name_matches, to_list, error_response,
)

LOG = logging.getLogger('cassette_actions')
MAX_IDS = 30
MAX_ALBUMS = 120
MAX_EXPANDED_TRACKS = 5000
CHUNK = 200


def get_pairs(request, body: bytes):
    pairs = list(request.query_params.multi_items())
    if body:
        if request.headers.get('content-type','').split(';')[0].lower().strip() != 'application/x-www-form-urlencoded':
            raise PersonalizationError('Expected Subsonic form parameters', 70)
        try:
            pairs.extend(parse_qsl(body.decode('utf-8'),keep_blank_values=True))
        except UnicodeDecodeError as exc:
            raise PersonalizationError('Invalid form encoding') from exc
    return pairs


def targets(pairs):
    extracted = []
    seen = set()
    for k,v in pairs:
        if k in ('id','albumId','artistId') and v:
            if len(v) > 512:
                raise PersonalizationError('Invalid media identifier')
            item = (k, v)
            if item not in seen:
                extracted.append(item)
                seen.add(item)
    if len(extracted) > MAX_IDS:
        raise PersonalizationError('Too many items in one favorite action')
    return extracted


async def expand_targets(client, upstream, auth, items):
    """Fetch all IDs before modifying favorites; never perform a partial expansion."""
    ids = []
    seen = set()
    semaphore = asyncio.Semaphore(8)

    async def fetch_album(album_id):
        async with semaphore:
            root = await nav_json(client, upstream, 'getAlbum', auth, {'id':album_id})
        album = root.get('album') or {}
        tracks = to_list(album.get('song'))
        if str(album.get('id','')) != album_id:
            raise PersonalizationError('Unable to verify album identity')
        return [str(t['id']) for t in tracks if isinstance(t,dict) and t.get('id')]

    albums = []
    for key, value in items:
        if key == 'id':
            if value not in seen:
                seen.add(value)
                ids.append(value)
        elif key == 'albumId':
            albums.append(value)
        else:
            raise PersonalizationError('Artist stars do not add to My Music')
    albums = list(dict.fromkeys(albums))
    if len(albums) > MAX_ALBUMS:
        raise PersonalizationError('Request exceeds safe album limit')
    album_tracks = await asyncio.gather(*(fetch_album(album) for album in albums))
    for batch in album_tracks:
        for song_id in batch:
            if song_id not in seen:
                seen.add(song_id)
                ids.append(song_id)
                if len(ids) > MAX_EXPANDED_TRACKS:
                    raise PersonalizationError('Request exceeds safe track limit')
    return ids


async def playlist_for_user(client,upstream,auth,create):
    user = auth['u']
    root = await nav_json(client,upstream,'getPlaylists',auth)
    playlists = to_list((root.get('playlists') or {}).get('playlist'))
    mine = [p for p in playlists if is_personal_playlist(p.get('name'),user)
            and name_matches(p.get('owner'),user)]
    if len(mine) > 1:
        raise PersonalizationError('Duplicate owned My Music playlists; rename duplicates')
    if not mine and create:
        await nav_json(client,upstream,'createPlaylist',auth,{'name':personal_playlist_name(user),'public':'false'})
        root = await nav_json(client,upstream,'getPlaylists',auth)
        playlists = to_list((root.get('playlists') or {}).get('playlist'))
        mine = [p for p in playlists if is_personal_playlist(p.get('name'),user)
                and name_matches(p.get('owner'),user)]
    if len(mine) != 1:
        if not mine and not create:
            return None, []
        raise PersonalizationError('Could not verify a private My Music playlist')
    playlist = mine[0]
    if not name_matches(playlist.get('name'), personal_playlist_name(user)):
        await nav_json(client, upstream, 'updatePlaylist', auth, {
            'playlistId':str(playlist['id']), 'name':personal_playlist_name(user), 'public':'false'})
    pid = str(playlist.get('id') or '')
    if not pid:
        raise PersonalizationError('Invalid My Music playlist ID')
    detail = await nav_json(client,upstream,'getPlaylist',auth,{'id':pid})
    data = detail.get('playlist') or {}
    if str(data.get('id')) != pid or not name_matches(data.get('owner'),user):
        raise PersonalizationError('Playlist ownership could not be verified', 40)
    return pid, to_list(data.get('entry'))


async def update_playlist(client,upstream,auth,pid,operation,values):
    """POST a standard OpenSubsonic updatePlaylist, validating response envelope."""
    for start in range(0,len(values),CHUNK):
        subset = values[start:start+CHUNK]
        pairs = [('playlistId',pid)] + [(operation,str(value)) for value in subset]
        try:
            response = await client.post(f'{upstream}/rest/updatePlaylist.view', params=auth,
                content=urlencode(pairs).encode(), headers={'Content-Type':'application/x-www-form-urlencoded'},
                timeout=30.0)
            response.raise_for_status()
            root = response.json().get('subsonic-response',{})
        except (httpx.HTTPError,ValueError,TypeError) as exc:
            raise PersonalizationError('My Music playlist update failed') from exc
        if root.get('status') != 'ok':
            raise PersonalizationError('Navidrome rejected the My Music playlist update')


async def sync_membership(request, upstream, auth, selected_ids):
    """Add songs to the signed-in user's My Music playlist, never remove any.

    Star/unstar state and personal collection membership are independent:
    a star saves tracks, but removing a star leaves all saved tracks intact.
    """
    client = request.app.state.client
    if not selected_ids:
        return
    pid, entries = await playlist_for_user(client, upstream, auth, create=True)
    current = {str(entry.get('id')) for entry in entries
               if isinstance(entry, dict) and entry.get('id')}
    missing = [song_id for song_id in selected_ids if song_id not in current]
    if missing:
        await update_playlist(client, upstream, auth, pid, 'songIdToAdd', missing)
    request.app.state.personal_cache.clear()


def is_valid_success(response):
    if response.status_code < 200 or response.status_code >= 300:
        return False
    try:
        data = response.json().get('subsonic-response') or {}
    except (ValueError, TypeError):
        return False
    return data.get('status') == 'ok'


async def handle_favorite(request, upstream, endpoint, body, clean_headers, rewritten=None):
    """Mirror actual Navidrome favorite changes into durable per-profile collection state.
    External catalog stars submit explicit acquisition requests to the Bridge.
    """
    from collection import change
    from discovery import is_external_id, request_external
    kind = endpoint.removesuffix('.view')
    if kind not in ('star', 'unstar'):
        return None
    try:
        pairs = rewritten[2] if rewritten is not None else get_pairs(request, body)
        for key in ('u','t','s','p','apiKey','f'):
            if sum(k == key for k,_ in pairs) > 1:
                raise PersonalizationError('Duplicate authentication or format parameter')
        params = dict(pairs)
        fmt = (params.get('f') or 'xml').lower()
        if fmt != 'json':
            raise PersonalizationError('Personal favorites require f=json')
        auth = credential_params(params)
        items = targets(pairs)
        if not items:
            raise PersonalizationError('Missing favorite target')
        # CASSETTE_ART_ALBUM_V6: external albums belong to the Bridge request
        # path. Never send their IDs to Navidrome's native star endpoint.
        from album_requests_v6 import external_album, star_external_album, unstar_external_album
        external_albums=[v for k,v in items if k=='albumId' and external_album(v)]
        if external_albums:
            if len(items)!=1:
                raise PersonalizationError('External album favorite requires one album')
            await nav_json(request.app.state.client,upstream,'ping',auth)
            async with request.app.state.star_lock:
                return await (star_external_album if kind=='star' else unstar_external_album)(
                    request,upstream,auth,external_albums[0])
        # Mixed local/external operations are rejected instead of partially applied.
        external = [v for k,v in items if k == 'id' and is_external_id(v)]
        if external:
            if len(items) != 1:
                raise PersonalizationError('External favorites require one song')
            # Validate the caller against Navidrome, not just a supplied username.
            await nav_json(request.app.state.client, upstream, 'ping', auth)
            async with request.app.state.star_lock:
                if kind == 'star':
                    return await request_external(request, upstream, auth, external[0])
                from saved_favorites import unsave_song
                unsave_song(auth['u'],external[0])
                return JSONResponse(
                    {'subsonic-response':{'status':'ok','version':'1.16.1','type':'navidrome'}},
                    headers={'Cache-Control':'private, no-store'})
        if any(is_external_id(v) for _,v in items):
            raise PersonalizationError('Unstarring unavailable requests is not supported')
        if len(items)==1 and items[0][0]=='albumId':
            from album_requests_v6 import partial_native_album
            remote_id=await partial_native_album(request,upstream,auth,items[0][1])
            if remote_id:
                async with request.app.state.star_lock:
                    response=await (star_external_album if kind=='star' else unstar_external_album)(request,upstream,auth,remote_id)
                    if response.status_code == 200 and json.loads(response.body).get('subsonic-response',{}).get('status') == 'ok':
                        from saved_favorites import remember_native_alias,drop_native_alias
                        import discovery as d
                        with d.connect() as db:
                            row=db.execute('SELECT mbid FROM discovery_albums WHERE id=?',(remote_id,)).fetchone()
                        if row and row[0]:
                            if kind=='star':
                                remember_native_alias(auth['u'],row[0],items[0][1])
                            else:
                                drop_native_alias(auth['u'],row[0])
                    return response
            if kind=='star':
                # No uniquely verified external release was found. Do not reject
                # the album favorite: forward the ordinary native star below,
                # which preserves the existing tracks in this user's music.
                # Never fabricate missing recordings or submit a guessed album.
                LOG.info('Album favorite using native tracks; exact release metadata unavailable')
        async with request.app.state.star_lock:
            path = f'{upstream}/rest/{endpoint}'
            raw_query = rewritten[0].decode('ascii') if rewritten else request.scope.get('query_string',b'').decode('ascii')
            url = path + ('?' + raw_query if raw_query else '')
            headers = clean_headers(request.headers)
            if rewritten:
                headers = [(k,v) for k,v in headers if k.lower() not in ('content-length','content-encoding')]
            outgoing = request.app.state.client.build_request(request.method,url,headers=headers,
                content=(rewritten[1] if rewritten else body) if request.method=='POST' else None)
            try:
                result = await request.app.state.client.send(outgoing,stream=False)
            except (httpx.HTTPError,RuntimeError) as exc:
                raise PersonalizationError('Navidrome favorite request failed') from exc
            clean = [(k.encode('latin-1'),v.encode('latin-1')) for k,v in clean_headers(result.headers)
                     if k.lower() not in ('content-length','content-encoding')]
            reply = Response(content=result.content,status_code=result.status_code)
            reply.raw_headers=clean
            if not is_valid_success(result):
                return reply
            saved = [(k,v) for k,v in items if k in ('id','albumId')]
            if saved:
                await change(request,upstream,auth,kind,saved)
            return reply
    except PersonalizationError as exc:
        LOG.warning('Favorite action rejected: %s',str(exc))
        return error_response(str(exc),exc.code)
    except Exception:
        LOG.exception('Favorite action error; inspect playlist state before retry')
        return error_response('Favorite applied but collection sync failed. Retry the action.',70,status_code=502)
