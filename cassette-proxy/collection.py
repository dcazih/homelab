"""Durable personal My Music semantics: explicit song stars, album bulk selections,
and per-song exclusions. Album unstar removes album-added songs from My Music while preserving individual stars.
Only a user's authenticated OpenSubsonic calls may mutate their collection.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager

from personal import PersonalizationError, to_list, nav_json
import httpx
from actions import playlist_for_user, update_playlist, is_valid_success

LOG = logging.getLogger('cassette_collection')
DB = os.getenv('CASSETTE_STATE_DB', '/data/cassette-state.sqlite')


def initialize():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS managed (username TEXT NOT NULL, song TEXT NOT NULL, PRIMARY KEY(username,song));
        CREATE TABLE IF NOT EXISTS individual (username TEXT NOT NULL, song TEXT NOT NULL, PRIMARY KEY(username,song));
        CREATE TABLE IF NOT EXISTS albums (username TEXT NOT NULL, album TEXT NOT NULL, PRIMARY KEY(username,album));
        CREATE TABLE IF NOT EXISTS exclusions (username TEXT NOT NULL, song TEXT NOT NULL, PRIMARY KEY(username,song));
        CREATE TABLE IF NOT EXISTS album_songs (username TEXT NOT NULL, album TEXT NOT NULL, song TEXT NOT NULL,
            PRIMARY KEY(username,album,song));
        ''')


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    try:
        db.execute('PRAGMA busy_timeout=15000')
        yield db
        db.commit()
    finally:
        db.close()


def stored(db, table, user, field):
    return {x[0] for x in db.execute(f'SELECT {field} FROM {table} WHERE username=?', (user,))}


async def bridge_claimed_ids(client, username):
    """Reject ledger migration on a Bridge transport failure rather than
    turning album-only membership into irrevocable individual ownership.
    """
    import discovery
    if not discovery.BRIDGE_KEY:
        return set()
    try:
        response=await client.post(discovery.BRIDGE+'/v1/internal/cassette/album-membership',
            json={'username':username},
            headers={'X-Cassette-Internal-Key':discovery.BRIDGE_KEY},timeout=6.0)
        response.raise_for_status()
        values=response.json().get('song_ids')
        if not isinstance(values,list):raise ValueError('Invalid membership response')
        return {str(x) for x in values if isinstance(x,str)}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise PersonalizationError('Album ownership verification unavailable; retry') from exc


async def bootstrap(db, client, upstream, auth):
    """Treat all pre-existing My Music tracks as individually retained on first use.
    This non-destructive migration avoids dropping songs saved in earlier stages.
    """
    user = auth['u'].casefold()
    if db.execute('SELECT 1 FROM users WHERE username=?', (user,)).fetchone():
        return
    _, entries = await playlist_for_user(client, upstream, auth, create=False)
    album_claimed = await bridge_claimed_ids(client, auth['u'])
    for entry in entries:
        if isinstance(entry, dict) and entry.get('id') and str(entry['id']) not in album_claimed:
            db.execute('INSERT OR IGNORE INTO individual VALUES (?,?)', (user, str(entry['id'])))
    db.execute('INSERT INTO users VALUES (?)', (user,))
    db.commit()


def desired(db, user):
    ids = stored(db, 'individual', user, 'song')
    ids.update(x[0] for x in db.execute('SELECT DISTINCT song FROM album_songs WHERE username=?',(user,)))
    return ids - stored(db, 'exclusions', user, 'song')


async def sync(db, request, upstream, auth):
    """Reconcile membership using server playlist indexes, not positional assumptions.
    Removal indexes are applied highest-first, with no stale-index batches.
    """
    client = request.app.state.client
    user = auth['u'].casefold()
    bridge_claims = await bridge_claimed_ids(client,auth['u'])
    wanted = desired(db, user) | bridge_claims
    pid, entries = await playlist_for_user(client, upstream, auth, create=bool(wanted))
    existing = [str(x.get('id')) for x in entries if isinstance(x, dict) and x.get('id')]
    if not pid:
        request.app.state.personal_cache.clear()
        return
    # Preserve music added independently by Bridge/Muse after proxy initialization.
    managed = stored(db,'managed',user,'song')
    externally_added = set(existing) - wanted - managed - bridge_claims
    if externally_added:
        db.executemany('INSERT OR IGNORE INTO individual VALUES (?,?)',[(user,s) for s in externally_added])
        db.commit()
        wanted.update(externally_added)
    seen = set()
    remove=[]
    for i,sid in enumerate(existing):
        if sid not in wanted or sid in seen:remove.append(i)
        seen.add(sid)
    for index in sorted(remove, reverse=True):
        await update_playlist(client, upstream, auth, pid, 'songIndexToRemove', [index])
    current = set(existing) & wanted
    add = sorted(wanted - current)
    if add:
        await update_playlist(client, upstream, auth, pid, 'songIdToAdd', add)
    request.app.state.personal_cache.clear()


async def change(request, upstream, auth, action, pairs):
    """Update durable selection and My Music. Called under the app-wide star lock
    after Navidrome's star/unstar endpoint accepted the request.
    """
    client = request.app.state.client
    user = auth['u'].casefold()
    songs = list(dict.fromkeys(v for k, v in pairs if k == 'id' and v))
    albums = list(dict.fromkeys(v for k, v in pairs if k == 'albumId' and v))
    if len(songs)+len(albums) > 30 or len(albums) > 15:
        raise PersonalizationError('Too many favorite targets')
    # Prefetch while the database is unchanged, validating real album identities.
    album_map = {}
    for album_id in albums:
        data = await nav_json(client, upstream, 'getAlbum', auth, {'id': album_id})
        album = data.get('album') or {}
        if str(album.get('id','')) != album_id:
            raise PersonalizationError('Invalid album target')
        tracks = [str(s['id']) for s in to_list(album.get('song')) if isinstance(s,dict) and s.get('id')]
        if not tracks or len(tracks) > 5000:
            raise PersonalizationError('Album contains no valid songs or exceeds track limit')
        album_map[album_id] = tracks
    with connect() as db:
        await bootstrap(db, client, upstream, auth)
        db.executemany('INSERT OR IGNORE INTO managed VALUES (?,?)',[(user,s) for s in songs])
        for album_id, track_ids in album_map.items():
            db.executemany('INSERT OR IGNORE INTO managed VALUES (?,?)',[(user,s) for s in track_ids])
            if action == 'star':
                db.execute('INSERT OR IGNORE INTO albums VALUES (?,?)', (user, album_id))
                db.execute('DELETE FROM album_songs WHERE username=? AND album=?', (user, album_id))
                db.executemany('INSERT OR IGNORE INTO album_songs VALUES (?,?,?)',
                               [(user, album_id, sid) for sid in track_ids])
                db.executemany('DELETE FROM exclusions WHERE username=? AND song=?',
                               [(user, sid) for sid in track_ids])
            else:
                db.execute('DELETE FROM albums WHERE username=? AND album=?', (user,album_id))
                db.execute('DELETE FROM album_songs WHERE username=? AND album=?', (user,album_id))
        for sid in songs:
            if action == 'star':
                db.execute('INSERT OR IGNORE INTO individual VALUES (?,?)', (user,sid))
                db.execute('DELETE FROM exclusions WHERE username=? AND song=?', (user,sid))
            else:
                db.execute('DELETE FROM individual WHERE username=? AND song=?', (user,sid))
                db.execute('INSERT OR IGNORE INTO exclusions VALUES (?,?)', (user,sid))
        db.commit()  # Save intention before network calls. Subsequent star retries will resync.
        await sync(db, request, upstream, auth)
