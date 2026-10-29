"""One verified recording/edition submission shared across Bridge profiles.

The claim is written before the remote POST. Uncertain outcomes remain claimed
for review so a timeout cannot trigger a duplicate download. Each profile keeps
its own request record and is fulfilled by the existing Navidrome reconciliation.
"""
import time


def initialize(connection):
    with connection() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS shared_track_acquisitions (
            recording_mbid TEXT NOT NULL, release_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, state TEXT NOT NULL, dn_task_id TEXT,
            created INTEGER NOT NULL, updated INTEGER NOT NULL,
            PRIMARY KEY(recording_mbid,release_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS shared_track_followers (
            request_id TEXT PRIMARY KEY, request_kind TEXT NOT NULL,
            recording_mbid TEXT NOT NULL, release_id TEXT NOT NULL,
            created INTEGER NOT NULL)''')


def follow(connection, request_id, request_kind, recording, release_id):
    with connection() as db:
        db.execute('''INSERT OR REPLACE INTO shared_track_followers VALUES (?,?,?,?,?)''',
                   (request_id,request_kind,recording.casefold(),
                    (release_id or '').casefold(),int(time.time())))


def follower_state(connection,request_id):
    with connection() as db:
        return db.execute('''SELECT a.state,a.updated FROM shared_track_followers f
            JOIN shared_track_acquisitions a ON a.recording_mbid=f.recording_mbid
              AND a.release_id=f.release_id WHERE f.request_id=?''',(request_id,)).fetchone()


def claim(connection, recording, release_id, owner_id, follower_kind=None):
    recording=recording.casefold(); release_id=(release_id or '').casefold()
    now=int(time.time())
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('''SELECT * FROM shared_track_acquisitions
            WHERE recording_mbid=? AND release_id=?''',(recording,release_id)).fetchone()
        if row and row['state']=='cancelling':
            return False,dict(row)
        if row and row['owner_id']==owner_id:
            return True,dict(row)
        if row and row['state'] in ('submitting','accepted','uncertain') and row['updated']>now-7*86400:
            if follower_kind:
                db.execute('INSERT OR REPLACE INTO shared_track_followers VALUES (?,?,?,?,?)',
                           (owner_id,follower_kind,recording,release_id,now))
            return False,dict(row)
        db.execute('''INSERT INTO shared_track_acquisitions VALUES (?,?,?,'submitting',NULL,?,?)
            ON CONFLICT(recording_mbid,release_id) DO UPDATE SET
            owner_id=excluded.owner_id,state='submitting',dn_task_id=NULL,
            created=excluded.created,updated=excluded.updated''',
            (recording,release_id,owner_id,now,now))
        return True,{'owner_id':owner_id,'state':'submitting','dn_task_id':None}


def finish(connection, recording, release_id, owner_id, state, dn_task_id=None):
    if state not in ('accepted','rejected','uncertain'):
        raise ValueError('Invalid acquisition state')
    with connection() as db:
        db.execute('''UPDATE shared_track_acquisitions SET state=?,dn_task_id=?,updated=?
          WHERE recording_mbid=? AND release_id=? AND owner_id=?''',
          (state,dn_task_id,int(time.time()),recording.casefold(),
           (release_id or '').casefold(),owner_id))
