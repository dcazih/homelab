"""Context-sensitive albums using explicit Home virtual IDs.

Personal Home album IDs encode the underlying Navidrome ID. Artist/search
pages retain native/external IDs, and therefore show verified full tracklists.
The proxy cannot infer arbitrary client navigation state from an ordinary
getAlbum call. These distinct IDs carry context without a Cassette UI fork.
"""
import logging
import sqlite3
from fastapi.responses import JSONResponse
from personal import (PersonalizationError, nav_json, personal_membership,
                      credential_params, request_params, to_list)
from virtual import decode_id

LOG = logging.getLogger('cassette.album_pages_stage5')


def _norm(value):
    from discovery import normalized
    return normalized(value)


def _json(root, view):
    return JSONResponse({'subsonic-response': root},
                        headers={'Cache-Control':'private, no-store',
                                 'X-Cassette-Album-View':view})


async def native_album_detail(request, upstream, body):
    parsed = request_params(request, body)
    album_id = parsed.get('id', '')
    scope = decode_id(album_id)
    if scope is not None and scope[0] != 'album':
        raise PersonalizationError('Album identifier is not an album')
    # A virtual album comes ONLY from a personal collection list. Other
    # album links must not inherit its filtering via mutable global state.
    personal = bool(scope) or parsed.get('cassetteScope') == 'personal'
    real_id = scope[1] if scope else album_id
    if not real_id or real_id.startswith('cassette-request-'):
        return None
    auth = credential_params(parsed)
    root = await nav_json(request.app.state.client, upstream, 'getAlbum', auth, {'id':real_id})
    album = root.get('album') or {}
    if str(album.get('id') or '') != real_id:
        raise PersonalizationError('Unknown native album')
    album = dict(album)
    local = [dict(s) for s in to_list(album.get('song')) if isinstance(s,dict) and s.get('id')]
    if personal:
        songs, _ = await personal_membership(request, upstream, auth)
        wanted = {str(s.get('id')) for s in songs}
        visible = [s for s in local if str(s['id']) in wanted]
        # Prefer entry contents when a Navidrome album was relabeled since save.
        album.update(id=album_id if scope else real_id, song=visible,
                     songCount=len(visible),
                     duration=sum(int(s.get('duration') or 0) for s in visible))
        # An album requested through Bridge is intentionally NOT starred in
        # Navidrome. Honor its user-scoped favorite ledger in Home, too, so
        # that Cassette can display an already-saved album as starred.
        # Never borrow another user's starred state.
        from album_requests_v6 import album_starred
        import discovery as d
        try:
            with d.connect() as db:
                db.row_factory = sqlite3.Row
                rows = [r['mbid'] for r in db.execute('SELECT mbid,name,artist FROM discovery_albums')
                        if _norm(r['name']) == _norm(album.get('name'))
                        and _norm(r['artist']) == _norm(album.get('artist'))
                        and d.MBID.fullmatch(str(r['mbid'] or ''))]
        except sqlite3.Error:
            # The optional discovery cache must not break personal album browsing.
            LOG.warning('Album favorite metadata cache unavailable on Home detail')
            rows = []
        groups = {g.lower() for g in rows}
        if len(groups) == 1:
            when = album_starred(auth['u'], next(iter(groups)))
            if when:
                album['starred'] = when
        root['album'] = album
        return _json(root, 'personal')

    # Existing Navidrome album is complete only if a verified external release
    # group corroborates it. We never join different known release groups.
    import discovery as d
    with d.connect() as db:
        db.row_factory=sqlite3.Row
        candidates=[dict(r) for r in db.execute('SELECT * FROM discovery_albums')
                    if _norm(r['name']) == _norm(album.get('name')) and
                       _norm(r['artist']) == _norm(album.get('artist')) and
                       d.MBID.fullmatch(str(r['mbid'] or ''))]
    if not candidates:
        # Native albums opened from search may never have passed an artist page.
        # Resolve against verified account-scoped album metadata on demand.
        try:
            from album_requests_v6 import partial_native_album
            await partial_native_album(request,upstream,auth,real_id)
            with d.connect() as db:
                db.row_factory=sqlite3.Row
                candidates=[dict(r) for r in db.execute('SELECT * FROM discovery_albums')
                    if _norm(r['name']) == _norm(album.get('name')) and
                       _norm(r['artist']) == _norm(album.get('artist')) and
                       d.MBID.fullmatch(str(r['mbid'] or ''))]
        except PersonalizationError:
            # No uniquely verifiable album edition: never invent tracks.
            pass
    by_group={r['mbid'].lower():r for r in candidates}
    if len(by_group)!=1:
        root['album']=album
        return _json(root, 'catalog-local-unverified')
    group=next(iter(by_group.values()))
    from album_requests_v6 import album_starred
    starred_at=album_starred(auth['u'],group['mbid'])
    if starred_at:album['starred']=starred_at
    catalog=await d.dn_json(request.app.state.client,auth['u'],'tracks',album_mbid=group['mbid'])
    remote = catalog.get('tracks') if isinstance(catalog,dict) else None
    if not isinstance(remote,list) or not remote:
        root['album']=album
        return _json(root, 'catalog-local-offline')
    # Match a local recording only when its recording MBID agrees. Fallback
    # title matching may display the wrong alternate/remix and is prohibited.
    local_by_mbid={}
    for entry in local:
        mbid=str(entry.get('musicBrainzId') or '').lower()
        if d.MBID.fullmatch(mbid):local_by_mbid.setdefault(mbid,[]).append(entry)
    seen=set()
    merged=[]
    for index, item in enumerate(remote[:500]):
        mbid=str(item.get('recording_id') or '').lower()
        title=item.get('title')
        if not d.MBID.fullmatch(mbid) or not title:continue
        options=[x for x in local_by_mbid.get(mbid,[]) if str(x['id']) not in seen]
        if not options:
            # For local files missing MusicBrainz recording tags, use only
            # a unique disc+position+title match for the *album display*.
            # This is NOT used by request fulfillment or virtual streaming.
            positional=[x for x in local
                if str(x['id']) not in seen
                and not str(x.get('musicBrainzId') or '').strip()
                and _norm(x.get('title'))==_norm(title)
                and int(x.get('discNumber') or 1)==int(item.get('disc_number') or 1)
                and int(x.get('track') or 0)==int(item.get('track_number') or index+1)]
            if len(positional)==1:options=positional
        if len(options)==1:
            song=dict(options[0]);seen.add(str(song['id']))
        else:
            song=d.add_candidate(mbid,group['artist'],title,group['name'],
                int((item.get('length') or 0)/1000),
                release_mbid=(catalog.get('selected_release_mbid') or ''),
                release_group_mbid=group['mbid'])
            if not song:continue
            song['albumId']=real_id
        song['discNumber']=int(item.get('disc_number') or 1)
        song['track']=int(item.get('track_number') or index+1)
        merged.append(song)
    # Keep Navidrome tracks without a recording MBID visible, but don't
    # misrepresent them as verified substitutes for exact remote recordings.
    for song in local:
        if str(song['id']) not in seen:merged.append(song)
    if merged:
        from saved_favorites import mark_songs
        mark_songs(auth['u'],merged)
        album.update(song=merged,songCount=len(merged),
                     duration=sum(int(s.get('duration') or 0) for s in merged))
        if not album.get('coverArt') and group.get('cover_id'):
            album['coverArt']=group['cover_id']
    root['album']=album
    return _json(root,'catalog-complete')
