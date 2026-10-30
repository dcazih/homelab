"""Durable album acquisition, partial fulfillment, and reversible per-user saves.

An album favorite is a *source of membership*. Unfavoriting removes only tracks
actually inserted by this album if no independent song/other-album claim exists.
Uncertain remote submission is never retried blindly. DB migrations are additive.
"""
import asyncio
import logging
import re
import sqlite3
import time
import httpx
from fastapi import Depends
from pydantic import BaseModel, Field
from urllib.parse import quote

import app as core

LOG = logging.getLogger('music_profiles.album_stage5')
MBID = re.compile(r'^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$')
_album_lock = asyncio.Lock()


def initialize():
    with core.connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS cassette_album_requests (
            user_id TEXT NOT NULL, release_group_mbid TEXT NOT NULL,
            artist TEXT NOT NULL, album TEXT NOT NULL,
            expected_tracks INTEGER NOT NULL, save_desired INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
            created INTEGER NOT NULL, updated INTEGER NOT NULL,
            PRIMARY KEY (user_id, release_group_mbid));
        CREATE TABLE IF NOT EXISTS cassette_album_track_links (
            user_id TEXT NOT NULL, release_group_mbid TEXT NOT NULL,
            song_id TEXT NOT NULL, inserted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, release_group_mbid, song_id));
        CREATE TABLE IF NOT EXISTS cassette_explicit_saves (
            user_id TEXT NOT NULL, song_id TEXT NOT NULL,
            PRIMARY KEY (user_id, song_id));
        ''')
        columns={row['name'] for row in db.execute('PRAGMA table_info(cassette_album_requests)')}
        if 'dismissed_at' not in columns:
            db.execute('ALTER TABLE cassette_album_requests ADD COLUMN dismissed_at INTEGER')
        # A pending HTTP response could have submitted on the remote server.
        db.execute("""UPDATE cassette_album_requests SET state='submission_uncertain',
          message='Bridge restarted during submission. Inspect remote request.',updated=?
          WHERE state='submitting'""",(int(time.time()),))
        db.execute("""UPDATE cassette_album_requests SET state='needs_review',
          message='Album preflight interrupted. Safe to review.',updated=?
          WHERE state='checking'""",(int(time.time()),))


initialize()


class AlbumRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    artist: str = Field(min_length=1, max_length=160)
    album: str = Field(min_length=1, max_length=200)
    release_group_mbid: str = Field(min_length=36, max_length=36)
    expected_tracks: int = Field(ge=1, le=1000)


class AlbumUnsave(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    release_group_mbid: str = Field(min_length=36, max_length=36)


def set_state(uid, rgid, state, message=''):
    with core.connection() as db:
        db.execute('''UPDATE cassette_album_requests SET state=?,message=?,updated=?
                      WHERE user_id=? AND release_group_mbid=?''',
                   (state,message,int(time.time()),uid,rgid))
    core.emit_live_request_status(uid,'album',rgid,state,message)


def album_matches(candidate, artist, album, rgid):
    if core.normalize(candidate.get('name')) != core.normalize(album):return False
    if core.normalize(candidate.get('artist')) != core.normalize(artist):return False
    candidate_mbid = str(candidate.get('musicBrainzId') or candidate.get('musicbrainzId') or '')
    if MBID.fullmatch(candidate_mbid) and candidate_mbid.lower() != rgid.lower():return False
    return True


async def find_album_tracks(user, artist, album, rgid):
    """Return existing tracks from ONE local album; never combine editions."""
    data = await core.sub(user,'search3',{'query':album,
        'songCount':'0','albumCount':'100','artistCount':'0'})
    rows=(data.get('searchResult3') or {}).get('album') or []
    if isinstance(rows,dict):rows=[rows]
    matches=[r for r in rows if isinstance(r,dict) and r.get('id') and
             album_matches(r,artist,album,rgid)]
    if len(matches)>1:
        # Two indistinguishable releases must be explicitly resolved.
        return None
    if not matches:return []
    album_id=str(matches[0]['id'])
    detail=(await core.sub(user,'getAlbum',{'id':album_id})).get('album') or {}
    if str(detail.get('id') or '') != album_id:return None
    entries=detail.get('song') or []
    if isinstance(entries,dict):entries=[entries]
    return list(dict.fromkeys(str(s['id']) for s in entries
                    if isinstance(s,dict) and s.get('id')))


async def find_complete_album(user, artist, album, rgid, expected):
    tracks=await find_album_tracks(user,artist,album,rgid)
    return tracks if tracks is not None and len(tracks)>=expected else None


async def save_album_tracks(user, group, track_ids):
    """Reconcile missing files with idempotent, per-track ownership records."""
    _, library = await core.playlist_ids(user)
    added = 0
    for sid in track_ids:
        with core.connection() as db:
            wanted=db.execute("SELECT save_desired FROM cassette_album_requests WHERE user_id=? AND release_group_mbid=?",
                              (user['id'],group)).fetchone()
            tracked=db.execute("SELECT inserted FROM cassette_album_track_links WHERE user_id=? AND release_group_mbid=? AND song_id=?",
                               (user['id'],group,sid)).fetchone()
        if not wanted or not wanted['save_desired']:break
        if tracked and sid in library:continue
        if sid in library:
            inserted=False  # An existing personal save isn't album-owned.
            actual_id=sid
        else:
            song,inserted=await core.append_to_my_music(user,sid)
            actual_id=str(song['id'])
            library.add(actual_id)
        if not tracked:
            with core.connection() as db:
                db.execute("INSERT OR IGNORE INTO cassette_album_track_links (user_id,release_group_mbid,song_id,inserted) VALUES (?,?,?,?)",
                           (user['id'],group,actual_id,int(inserted)))
        added += int(inserted)
    return added


async def _reconcile_one(user,row):
    group=row['release_group_mbid']
    tracks=await find_album_tracks(user,row['artist'],row['album'],group)
    if tracks is None:
        set_state(user['id'],group,'needs_review',
                  'Multiple local editions match; choose one before importing')
        return 0
    if tracks:
        await save_album_tracks(user,group,tracks)
        if len(tracks)>=row['expected_tracks']:
            set_state(user['id'],group,'ready',
                      f'All {len(tracks)} album tracks are indexed and saved')
        else:
            set_state(user['id'],group,'importing',
                      f'{len(tracks)}/{row["expected_tracks"]} songs indexed in Navidrome')
    return len(tracks)


@core.app.post('/v1/internal/cassette/album-request',
    dependencies=[Depends(core.require_cassette_internal)],include_in_schema=False)
async def request_album(payload:AlbumRequest):
    if not MBID.fullmatch(payload.release_group_mbid):core.err('Invalid album ID',400)
    with core.connection() as db:
        found=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',
                         (payload.username,)).fetchone()
    if not found:core.err('Connect this Navidrome user in Music Profiles first',404)
    user=dict(found);group=payload.release_group_mbid.lower()
    async with _album_lock:
        previously_active = None
        with core.connection() as db:
            row=db.execute('''SELECT * FROM cassette_album_requests WHERE user_id=?
                     AND release_group_mbid=?''',(user['id'],group)).fetchone()
            if row:
                # Existing remote requests and uncertain POSTs are NOT resubmitted.
                if row['state'] not in ('needs_link','needs_relink','needs_review','cancelled'):
                    db.execute('''UPDATE cassette_album_requests SET save_desired=1,dismissed_at=NULL,updated=?
                          WHERE user_id=? AND release_group_mbid=?''',
                          (int(time.time()),user['id'],group))
                    previously_active = dict(row)
                else:
                    db.execute('''UPDATE cassette_album_requests SET state='checking',save_desired=1,dismissed_at=NULL,
                         expected_tracks=MAX(expected_tracks,?),updated=?
                         WHERE user_id=? AND release_group_mbid=?''',
                         (payload.expected_tracks,int(time.time()),user['id'],group))
            else:
                now=int(time.time())
                db.execute('''INSERT INTO cassette_album_requests
                    (user_id,release_group_mbid,artist,album,expected_tracks,state,message,created,updated)
                    VALUES (?,?,?,?,?,'checking','Checking shared catalog',?,?)''',
                    (user['id'],group,payload.artist,payload.album,payload.expected_tracks,now,now))
        if previously_active:
            if previously_active['state'] in ('ready','added','importing','requested','awaiting_approval'):
                try: await _reconcile_one(user,previously_active)
                except Exception as exc:LOG.warning('Album refresh failed: %s',type(exc).__name__)
            with core.connection() as check:
                state=check.execute('''SELECT state,message FROM cassette_album_requests
                      WHERE user_id=? AND release_group_mbid=?''',(user['id'],group)).fetchone()
            return {'status':state['state'],'message':state['message']}
        try:
            current=await find_album_tracks(user,payload.artist,payload.album,group)
            if current is None:
                set_state(user['id'],group,'needs_review','Ambiguous local editions')
                return {'status':'needs_review'}
            if current:
                await save_album_tracks(user,group,current)
                if len(current)>=payload.expected_tracks:
                    set_state(user['id'],group,'ready','Album already available and saved')
                    return {'status':'ready','song_count':len(current)}
        except Exception as exc:
            LOG.warning('Album preflight failed: %s',type(exc).__name__)
            set_state(user['id'],group,'needs_review','Unable to verify current album')
            return {'status':'needs_review'}
        token=core.linked_token(core.connection,core.cipher,user['id'])
        if not token:
            set_state(user['id'],group,'needs_link','Connect your DroppedNeedle account')
            return {'status':'needs_link'}
        set_state(user['id'],group,'submitting','Submitting verified album request')
        try:
            async with httpx.AsyncClient(timeout=25,trust_env=False,follow_redirects=False) as client:
                response=await client.post(core.DN+'/api/v1/requests/new',
                    json={'musicbrainz_id':group,'artist':payload.artist,'album':payload.album},
                    headers={'Authorization':'Bearer '+token})
        except httpx.HTTPError:
            set_state(user['id'],group,'submission_uncertain','Remote result unknown; inspect before retrying')
            return {'status':'submission_uncertain'}
        if response.status_code==401:
            status='needs_relink';message='Reconnect your DroppedNeedle account'
        elif response.status_code==403:
            status='needs_admin';message='DroppedNeedle access denied'
        elif response.status_code>=400:
            status='needs_review';message=f'DroppedNeedle rejected request: HTTP {response.status_code}'
        else:
            try:
                result=response.json()
                if not isinstance(result,dict):raise ValueError('Unexpected response')
                state=str(result.get('status') or 'requested')
            except (ValueError,TypeError):
                set_state(user['id'],group,'submission_uncertain','Unclear server response')
                return {'status':'submission_uncertain'}
            status='awaiting_approval' if state in ('pending','awaiting_approval') else 'requested'
            message='Request accepted; waiting for import'
        set_state(user['id'],group,status,message)
        return {'status':status,'message':message}


async def reconcile_album_requests(user):
    async with _album_lock:
        with core.connection() as db:
            rows=[dict(x) for x in db.execute('''SELECT * FROM cassette_album_requests
                WHERE user_id=? AND save_desired=1 AND state IN
                ('requested','awaiting_approval','importing','ready','added')
                ORDER BY updated LIMIT 100''',(user['id'],))]
        total=0
        for row in rows:
            try:total+=int(bool(await _reconcile_one(user,row)))
            except Exception as exc:LOG.warning('Album reconcile failed: %s',type(exc).__name__)
        return total


@core.app.get('/v1/album-requests')
def list_album_requests(user=Depends(core.require_user)):
    with core.connection() as db:
        entries=[dict(r) for r in db.execute('''SELECT release_group_mbid,artist,album,state,message,
            expected_tracks,save_desired,created,updated FROM cassette_album_requests
            WHERE user_id=? AND dismissed_at IS NULL ORDER BY created DESC LIMIT 200''',(user['id'],))]
        for row in entries:
            row['display_status']=core.request_status(row['state'])
            row['saved_tracks']=db.execute('''SELECT COUNT(*) FROM cassette_album_track_links
               WHERE user_id=? AND release_group_mbid=?''',
               (user['id'],row['release_group_mbid'])).fetchone()[0]
    return {'requests':entries}


async def _individually_saved(user, song_ids):
    """Only verified native stars and explicit independent Bridge saves survive."""
    starred=await core.sub(user,'getStarred2')
    own=(starred.get('starred2') or {}).get('song') or []
    if isinstance(own,dict):own=[own]
    ids={str(s['id']) for s in own if isinstance(s,dict) and s.get('id')}
    with core.connection() as db:
        ids.update(r[0] for r in db.execute('''SELECT song_id FROM cassette_explicit_saves
                    WHERE user_id=?''',(user['id'],)))
        ids.update(str(r[0]) for r in db.execute('''SELECT nav_song_id FROM proposals
              WHERE user_id=? AND source='Cassette star' AND nav_song_id IS NOT NULL
                AND state='added' ''',(user['id'],)))
    return ids.intersection(song_ids)


async def unstar_album(payload:AlbumUnsave):
    if not MBID.fullmatch(payload.release_group_mbid):core.err('Invalid album ID',400)
    with core.connection() as db:
        found=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',
                         (payload.username,)).fetchone()
    if not found:core.err('Unknown Music Profiles user',404)
    user=dict(found);group=payload.release_group_mbid.lower()
    async with _album_lock:
        # Persist intent BEFORE attempting any remote update. Future imports
        # must not re-add a cancelled album, even if removal partially fails.
        with core.connection() as db:
            db.execute('''UPDATE cassette_album_requests SET save_desired=0,updated=?
                         WHERE user_id=? AND release_group_mbid=?''',
                         (int(time.time()),user['id'],group))
            tracks=[dict(r) for r in db.execute('''SELECT song_id,inserted
                FROM cassette_album_track_links WHERE user_id=? AND release_group_mbid=?''',
                (user['id'],group))]
            others={str(r[0]) for r in db.execute('''SELECT t.song_id
                FROM cassette_album_track_links t JOIN cassette_album_requests a
                  ON a.user_id=t.user_id AND a.release_group_mbid=t.release_group_mbid
                WHERE t.user_id=? AND t.release_group_mbid!=? AND a.save_desired=1''',
                (user['id'],group))}
        ids={r['song_id'] for r in tracks}
        independently=await _individually_saved(user,ids)
        remove={r['song_id'] for r in tracks if r['inserted'] and
                r['song_id'] not in independently and r['song_id'] not in others}
        if remove:
            pid,existing=await core.playlist_ids(user)
            # Apply descending song indexes to avoid shifting subsequent rows.
            detail=(await core.sub(user,'getPlaylist',{'id':pid})).get('playlist') or {}
            entries=detail.get('entry') or []
            if isinstance(entries,dict):entries=[entries]
            for index in reversed(range(len(entries))):
                if str(entries[index].get('id') or '') in remove:
                    await core.sub(user,'updatePlaylist',{
                        'playlistId':pid,'songIndexToRemove':str(index)})
        with core.connection() as db:
            db.execute('''DELETE FROM cassette_album_track_links
                WHERE user_id=? AND release_group_mbid=?''',(user['id'],group))
        return {'status':'unstarred','removed_count':len(remove),
                'preserved_individual':len(independently)}


core.app.post('/v1/internal/cassette/album-unsave',
    dependencies=[Depends(core.require_cassette_internal)],
    include_in_schema=False)(unstar_album)


class AlbumMembershipLookup(BaseModel):
    username: str = Field(min_length=1, max_length=80)


@core.app.post('/v1/internal/cassette/album-membership',
    dependencies=[Depends(core.require_cassette_internal)],include_in_schema=False)
def album_membership(body: AlbumMembershipLookup):
    """Expose active album claims to the proxy's independent favorite ledger.

    This prevents proxy bootstrap from accidentally promoting Bridge-added
    album tracks to independently owned/favorited songs.
    """
    with core.connection() as db:
        user = db.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',
                          (body.username,)).fetchone()
        if not user:return {'song_ids':[]}
        rows = db.execute('''SELECT DISTINCT t.song_id FROM cassette_album_track_links t
               JOIN cassette_album_requests a ON a.user_id=t.user_id
                   AND a.release_group_mbid=t.release_group_mbid
               WHERE t.user_id=? AND a.save_desired=1''',(user['id'],))
        return {'song_ids':[str(x[0]) for x in rows]}
