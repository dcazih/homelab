"""Bounded Bridge metadata rechecks for already-approved requests.

Only proposals in needs_metadata (produced by a previous explicit Approve/check)
are eligible. Never retries a POST with uncertain outcome, submitting, requested,
or added state. Uses CAS reservation and the existing verified metadata resolver.
"""
import logging
import time
import app as core

LOG = logging.getLogger('music_profiles.metadata_recovery')
DELAYS = (15*60, 60*60, 4*3600, 24*3600)


def initialize():
    with core.connection() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS metadata_recovery_v1 (
            proposal_id TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL DEFAULT 0
        )''')


initialize()


def reserve_due(proposal_id, user_id):
    """Atomically claim one eligible recheck even with concurrent workers."""
    now=time.time()
    with core.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        proposal=db.execute('SELECT state FROM proposals WHERE id=? AND user_id=?',
                            (proposal_id,user_id)).fetchone()
        if not proposal or proposal['state']!='needs_metadata':
            return False
        existing=db.execute('SELECT attempts,next_at FROM metadata_recovery_v1 WHERE proposal_id=?',
                            (proposal_id,)).fetchone()
        attempts=int(existing['attempts']) if existing else 0
        next_at=float(existing['next_at']) if existing else 0
        if attempts>=len(DELAYS) or next_at>now:
            return False
        db.execute('''INSERT INTO metadata_recovery_v1(proposal_id,attempts,next_at)
                      VALUES(?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET
                      attempts=excluded.attempts,next_at=excluded.next_at''',
                   (proposal_id,attempts+1,now+DELAYS[attempts]))
        return True


async def recheck_for_user(user):
    """Recheck at most one metadata failure per user per reconciliation tick."""
    with core.connection() as db:
        rows=db.execute('''SELECT p.* FROM proposals p
               LEFT JOIN metadata_recovery_v1 mr ON mr.proposal_id=p.id
               WHERE p.user_id=? AND p.state='needs_metadata'
                 AND COALESCE(mr.attempts,0)<?
                 AND COALESCE(mr.next_at,0)<=?
               ORDER BY p.created,p.id LIMIT 1''',
                        (user['id'],len(DELAYS),time.time())).fetchall()
    successes=0
    for row in rows:
        p=dict(row)
        if not reserve_due(p['id'],user['id']):
            continue
        try:
            exact,_=await core.find_proposal_matches(user,p)
            if len(exact)==1:
                song,_=await core.append_to_my_music(user,str(exact[0]['id']))
                core.update_prop(user['id'],p['id'],'added',
                                 'Existing shared recording automatically added to My Music',str(song['id']))
                successes+=1
            elif len(exact)>1:
                core.update_prop(user['id'],p['id'],'needs_selection',
                                 'Several library recordings match; select the exact version')
            else:
                await core._submit_verified_cassette_track(user,p['id'],p)
        except Exception as exc:
            LOG.warning('Metadata recovery skipped request %s (%s)',p['id'],type(exc).__name__)
    return successes
