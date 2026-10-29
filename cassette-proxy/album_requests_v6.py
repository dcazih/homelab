"""User-scoped external album stars through Music Profiles Bridge.

This only handles album IDs from discovery_albums. Native album favorite
behavior remains in actions/collection unchanged.
"""
import logging
import sqlite3
import httpx
from fastapi.responses import JSONResponse
from personal import PersonalizationError, nav_json, error_response

LOG = logging.getLogger('cassette.album_request_v6')


def external_album(value):
    return isinstance(value, str) and value.startswith('cassette-request-album_')


async def star_external_album(request, upstream, auth, album_id):
    import discovery as d
    with d.connect() as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM discovery_albums WHERE id=?', (album_id,)).fetchone()
    if row is None or not d.MBID.fullmatch(str(row['mbid'] or '')):
        raise PersonalizationError('Album has no verified MusicBrainz release-group ID')
    # Star state is durable independent of availability. When metadata cannot
    # verify a release yet, the album is still saved but no download is claimed.
    await nav_json(request.app.state.client, upstream, 'ping', auth)
    remember_album_star(auth['u'], row['mbid'], True)
    request.app.state.personal_cache.clear()
    status='needs_metadata'
    if d.BRIDGE_KEY:
        try:
            catalog = await d.dn_json(request.app.state.client, auth['u'], 'tracks', album_mbid=row['mbid'])
        except (httpx.HTTPError, ValueError, TypeError):
            catalog = None
            LOG.warning('Album starred; track catalog unavailable')
        entries = catalog.get('tracks') if isinstance(catalog, dict) else None
        verified = [t for t in entries if isinstance(t,dict) and d.MBID.fullmatch(str(t.get('recording_id') or ''))] if isinstance(entries,list) else []
        if verified:
            body = {'username': auth['u'], 'artist': row['artist'],
                    'album': row['name'], 'release_group_mbid': row['mbid'],
                    'expected_tracks': len(verified)}
            try:
                res = await request.app.state.client.post(
                    d.BRIDGE + '/v1/internal/cassette/album-request', json=body,
                    headers={'X-Cassette-Internal-Key': d.BRIDGE_KEY}, timeout=30)
                res.raise_for_status()
                status=str(res.json().get('status') or 'bridge_unknown')
            except (httpx.HTTPError,ValueError,TypeError):
                status='bridge_unavailable'
                LOG.warning('Album starred but request submission unconfirmed')
    else:
        status='bridge_not_configured'
    return JSONResponse({'subsonic-response': {
        'status': 'ok', 'version': '1.16.1', 'type': 'navidrome'}},
        headers={'Cache-Control':'private, no-store',
                 'X-Cassette-Request-Status':status})


async def unstar_external_album(request, upstream, auth, album_id):
    import discovery as d
    if not d.BRIDGE_KEY:
        raise PersonalizationError('Music Profiles Bridge integration not configured')
    with d.connect() as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM discovery_albums WHERE id=?',(album_id,)).fetchone()
    if not row or not d.MBID.fullmatch(str(row['mbid'] or '')):
        raise PersonalizationError('Unknown verified external album')
    await nav_json(request.app.state.client,upstream,'ping',auth)
    try:
        response=await request.app.state.client.post(
            d.BRIDGE+'/v1/internal/cassette/album-unsave',
            json={'username':auth['u'],'release_group_mbid':row['mbid']},
            headers={'X-Cassette-Internal-Key':d.BRIDGE_KEY},timeout=15)
        response.raise_for_status()
        payload=response.json()
        if payload.get('status')!='unstarred':
            raise ValueError('Album unsave unconfirmed')
    except (httpx.HTTPError, ValueError, TypeError):
        raise PersonalizationError('Could not update album favorite; retry')
    remember_album_star(auth['u'], row['mbid'], False)
    request.app.state.personal_cache.clear()
    return JSONResponse({'subsonic-response':{
        'status':'ok','version':'1.16.1','type':'navidrome'}},
        headers={'Cache-Control':'private, no-store'})


def remember_album_star(username, group_id, starred):
    import discovery as d
    with d.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS external_album_favorites (
            username TEXT NOT NULL, release_group_mbid TEXT NOT NULL,
            starred INTEGER NOT NULL, starred_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(username,release_group_mbid))''')
        db.execute('''INSERT INTO external_album_favorites VALUES (?,?,?,?)
            ON CONFLICT(username,release_group_mbid) DO UPDATE SET
              starred=excluded.starred,starred_at=excluded.starred_at''',
            (username.casefold(),group_id.lower(),int(starred),
             __import__('time').time_ns()//1000000000 if starred else 0))


def album_starred(username, group_id):
    import discovery as d
    with d.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS external_album_favorites (
            username TEXT NOT NULL, release_group_mbid TEXT NOT NULL,
            starred INTEGER NOT NULL, starred_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(username,release_group_mbid))''')
        row=db.execute('''SELECT starred,starred_at FROM external_album_favorites
            WHERE username=? AND release_group_mbid=?''',
            (username.casefold(),group_id.lower())).fetchone()
    if not row or not row[0]:return None
    from datetime import datetime,timezone
    return datetime.fromtimestamp(row[1],timezone.utc).isoformat().replace('+00:00','Z')


async def partial_native_album(request, upstream, auth, native_id):
    """Resolve a local album to one exact group for star AND unstar."""
    import discovery as d
    local=(await nav_json(request.app.state.client,upstream,'getAlbum',auth,
                          {'id':native_id})).get('album') or {}
    if str(local.get('id'))!=str(native_id):
        raise PersonalizationError('Cannot verify local album')
    artist=d.normalized(local.get('artist'))
    album=d.normalized(local.get('name'))
    if not artist or not album:return None

    def known():
        with d.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM discovery_albums')
                    if d.normalized(row['artist'])==artist and
                       d.normalized(row['name'])==album and
                       d.MBID.fullmatch(str(row['mbid'] or ''))]
    candidates=known()
    if not candidates:
        catalog=await d.dn_json(request.app.state.client,auth['u'],'albums',
                                query=local.get('name') or '')
        if isinstance(catalog,dict):
            for item in list(catalog.get('albums') or [])+list(catalog.get('results') or []):
                if not isinstance(item,dict):continue
                title=item.get('title') or item.get('name')
                credit=item.get('artist') or item.get('artist_name')
                gid=item.get('musicbrainz_id')
                if (d.normalized(title)==album and d.normalized(credit)==artist
                        and d.MBID.fullmatch(str(gid or ''))):
                    d.remember_remote_album(title,credit,gid)
        candidates=known()
    if not candidates:
        # Fallback to an exact public MusicBrainz release-group lookup. MusicBrainz
        # imposes a shared request budget in discovery._artist_mb_get.
        try:
            lookup=await d._artist_mb_get(request.app.state.client,'release-group/',{
                'query':f'releasegroup:"{local.get("name", "")}" AND artist:"{local.get("artist", "")}"',
                'limit':20})
            for item in (lookup.get('release-groups') or []) if isinstance(lookup,dict) else []:
                if not isinstance(item,dict):continue
                credit=''.join(str(c.get('name') or c.get('artist',{}).get('name') or '')
                               if isinstance(c,dict) else str(c)
                               for c in (item.get('artist-credit') or []))
                if (d.MBID.fullmatch(str(item.get('id') or ''))
                    and d.normalized(item.get('title'))==album
                    and d.normalized(credit)==artist
                    and item.get('primary-type')=='Album'):
                    d.remember_remote_album(item['title'],local['artist'],item['id'])
            candidates=known()
        except Exception as exc:
            LOG.info('Exact album group metadata unavailable: %s',type(exc).__name__)
    groups={r['mbid'].lower():r for r in candidates}
    if len(groups)>1:
        raise PersonalizationError('Multiple album editions match; select the exact release')
    return next(iter(groups.values()))['id'] if groups else None
