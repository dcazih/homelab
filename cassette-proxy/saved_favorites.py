"""User-scoped virtual favorites, independent of Navidrome's real-library favorites.

Cassette periodically replaces its local favorite records with getStarred2.
A request being accepted by Music Profiles Bridge is not the same as a
Navidrome star, so virtual selections must also be returned by getStarred2.
"""
import time
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from collection import connect
from personal import nav_json, to_list


def initialize():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS cassette_virtual_song_stars (
            username TEXT NOT NULL, song_id TEXT NOT NULL, starred_at INTEGER NOT NULL,
            PRIMARY KEY(username,song_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS external_album_favorites (
            username TEXT NOT NULL, release_group_mbid TEXT NOT NULL,
            starred INTEGER NOT NULL, starred_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(username,release_group_mbid))''')
        db.execute('''CREATE TABLE IF NOT EXISTS cassette_album_star_aliases (
            username TEXT NOT NULL, release_group_mbid TEXT NOT NULL, native_id TEXT NOT NULL,
            PRIMARY KEY(username,release_group_mbid,native_id))''')


def when_epoch(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')


def save_song(username, song_id):
    with connect() as db:
        db.execute('''INSERT INTO cassette_virtual_song_stars (username,song_id,starred_at)
            VALUES (?,?,?) ON CONFLICT(username,song_id) DO NOTHING''',
            (username.casefold(),song_id,int(time.time())))


def unsave_song(username, song_id):
    with connect() as db:
        db.execute('DELETE FROM cassette_virtual_song_stars WHERE username=? AND song_id=?',
                   (username.casefold(),song_id))


def selected_song_dates(username):
    with connect() as db:
        return {sid:when_epoch(stamp) for sid,stamp in db.execute(
            'SELECT song_id,starred_at FROM cassette_virtual_song_stars WHERE username=?',
            (username.casefold(),))}


def mark_songs(username, songs):
    selected=selected_song_dates(username)
    for song in songs:
        if isinstance(song,dict) and song.get('id') in selected:
            song['starred']=selected[song['id']]
    return songs


def remember_native_alias(username,release_group_mbid,native_id):
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO cassette_album_star_aliases VALUES (?,?,?)',
                   (username.casefold(),release_group_mbid.lower(),str(native_id)))


def drop_native_alias(username,release_group_mbid):
    with connect() as db:
        db.execute('DELETE FROM cassette_album_star_aliases WHERE username=? AND release_group_mbid=?',
                   (username.casefold(),release_group_mbid.lower()))


async def merged_get_starred2(request,upstream,auth):
    """Return real favorites plus requested music under the same signed-in user.

    On state DB failures fail closed with an error, not an empty successful
    result, because Cassette treats an empty getStarred2 as 'delete favorites'.
    """
    import sqlite3
    import discovery as d
    from virtual import as_virtual_album
    
    root = await nav_json(request.app.state.client,upstream,'getStarred2',auth)
    starred = root.get('starred2') or {}
    if not isinstance(starred,dict):
        raise ValueError('Invalid upstream favorite response')
    songs=[dict(s) for s in to_list(starred.get('song')) if isinstance(s,dict)]
    albums=[dict(a) for a in to_list(starred.get('album')) if isinstance(a,dict)]
    saved=selected_song_dates(auth['u'])
    with connect() as db:
        db.row_factory=sqlite3.Row
        found=[dict(r) for r in db.execute('''SELECT d.* FROM discovery d
            INNER JOIN cassette_virtual_song_stars f ON d.id=f.song_id
            WHERE f.username=?''',(auth['u'].casefold(),))]
        album_rows=[dict(r) for r in db.execute('''SELECT a.*, f.starred_at
            FROM discovery_albums a JOIN external_album_favorites f
            ON LOWER(a.mbid)=f.release_group_mbid
            WHERE f.username=? AND f.starred=1''',(auth['u'].casefold(),))]
        saved_album_groups={r[0] for r in db.execute('''SELECT release_group_mbid
            FROM external_album_favorites WHERE username=? AND starred=1''',
            (auth['u'].casefold(),))}
        aliases=[tuple(r) for r in db.execute('''SELECT release_group_mbid,native_id
            FROM cassette_album_star_aliases WHERE username=?''',(auth['u'].casefold(),))]
    # Inconsistency must not silently erase an existing virtual favorite.
    if len(found)!=len(saved):
        raise ValueError('Virtual favorite metadata unavailable')
    if saved_album_groups - {str(row['mbid']).lower() for row in album_rows}:
        raise ValueError('Saved album metadata unavailable')
    existing={str(s.get('id')) for s in songs}
    for item in found:
        if item['id'] not in existing:
            candidate=d.external_song(item)
            candidate['starred']=saved[item['id']]
            songs.append(candidate)
            existing.add(item['id'])
    album_seen={str(a.get('id')) for a in albums}
    for row in album_rows:
        when=when_epoch(row['starred_at'])
        item={'id':row['id'],'name':row['name'],'artist':row['artist'],
              'songCount':row.get('song_count') or 0,'duration':0,'starred':when}
        if row.get('cover_id'):item['coverArt']=row['cover_id']
        if row['id'] not in album_seen:
            albums.append(item);album_seen.add(row['id'])
    # Native album links from Home are virtual IDs, but Cassette requires
    # identical IDs when it refreshes its locally persisted favorite records.
    # Preserve both catalog and Home aliases without changing real Navidrome IDs.
    by_group={row['mbid'].lower():row for row in album_rows if row.get('mbid')}
    for group_id,native_id in aliases:
        row=by_group.get(group_id)
        if not row:continue
        when=when_epoch(row['starred_at'])
        for album_id in (str(native_id),as_virtual_album({'id':str(native_id)})['id']):
            if album_id not in album_seen:
                item={'id':album_id,'name':row['name'],'artist':row['artist'],
                      'songCount':row.get('song_count') or 0,'duration':0,'starred':when}
                if row.get('cover_id'):item['coverArt']=row['cover_id']
                albums.append(item);album_seen.add(album_id)
    root['starred2']={**starred,'song':songs,'album':albums}
    return JSONResponse({'subsonic-response':root},headers={'Cache-Control':'private, no-store'})
