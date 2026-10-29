"""Durable peer admission and transfer ledger shared with the Bridge dashboard.

This module never weakens DroppedNeedle's matcher or importer. SQLite transactions
serialize admission across workers. A pending enqueue is reconciled with slskd
before reuse after a crash; an uncertain enqueue is never submitted twice.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB = os.getenv('PEER_SCHEDULER_DB', '/peer-scheduler-data/scheduler.sqlite')
DEFAULTS = {
    'active_batches_per_peer': 1, 'queued_albums_per_peer': 1,
    'queued_files_per_peer': 15, 'queued_mb_per_peer': 500,
    'daily_batches_per_peer': 2, 'batch_cooldown_seconds': 1800,
    'duplicate_search_cooldown_seconds': 1800,
    'pending_check_seconds': 300, 'retry_attempts': 3,
    'retry_backoff_seconds': 300, 'queue_timeout_seconds': 7200,
    'stalled_seconds': 1800, 'global_concurrency': 4,
    'search_interval_seconds': 15, 'candidate_cache_seconds': 1800,
    'auto_retries': True, 'alternative_sources': True, 'fairness': True,
    'paused': False,
}
BOOL_KEYS = {'auto_retries', 'alternative_sources', 'fairness', 'paused'}


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB, timeout=20)
    # Bridge (UID 10001) and DroppedNeedle (host PUID) share this one volume.
    # The installer makes its directory writable to both; the creator makes
    # the database writable before the other process opens it.
    try: os.chmod(DB,0o666)
    except (PermissionError,OSError): pass
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=20000')
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize():
    with connect() as db:
        db.execute('PRAGMA journal_mode=DELETE')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS peers (
          name TEXT PRIMARY KEY COLLATE NOCASE, disabled_until INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, rule_text TEXT NOT NULL DEFAULT '',
          last_activity INTEGER NOT NULL DEFAULT 0, last_completed INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
          files_downloaded INTEGER NOT NULL DEFAULT 0, bytes_transferred INTEGER NOT NULL DEFAULT 0,
          speed_sum REAL NOT NULL DEFAULT 0, speed_samples INTEGER NOT NULL DEFAULT 0,
          limits_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS batches (
          task_id TEXT PRIMARY KEY, peer TEXT NOT NULL COLLATE NOCASE,
          state TEXT NOT NULL, files_json TEXT NOT NULL, file_count INTEGER NOT NULL,
          bytes_queued INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
          created INTEGER NOT NULL, updated INTEGER NOT NULL, next_check INTEGER NOT NULL DEFAULT 0,
          reason TEXT NOT NULL DEFAULT '', progress REAL NOT NULL DEFAULT 0,
          queue_position INTEGER, speed REAL NOT NULL DEFAULT 0,
          last_bytes INTEGER NOT NULL DEFAULT 0, progress_at INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS batches_peer_state ON batches(peer,state);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL,
          peer TEXT NOT NULL COLLATE NOCASE, task_id TEXT, kind TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS events_peer_at ON events(peer,at DESC);
        CREATE TABLE IF NOT EXISTS search_cache (
          query TEXT PRIMARY KEY, created INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS search_gate (
          id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL NOT NULL);
        INSERT OR IGNORE INTO search_gate VALUES (1,0);
        CREATE TABLE IF NOT EXISTS acquisition_phases (
          task_id TEXT PRIMARY KEY, state TEXT NOT NULL, selected_peer TEXT NOT NULL DEFAULT '',
          alternatives_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '',
          created INTEGER NOT NULL, updated INTEGER NOT NULL);
        ''')
        cols={r[1] for r in db.execute('PRAGMA table_info(batches)')}
        if 'last_bytes' not in cols:db.execute('ALTER TABLE batches ADD COLUMN last_bytes INTEGER NOT NULL DEFAULT 0')
        if 'progress_at' not in cols:db.execute('ALTER TABLE batches ADD COLUMN progress_at INTEGER NOT NULL DEFAULT 0')
        for key, value in DEFAULTS.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key,json.dumps(value)))


def settings():
    with connect() as db:
        values = dict(DEFAULTS)
        values.update({r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM settings')})
        return values


def update_settings(changes):
    if not isinstance(changes,dict) or not changes:
        raise ValueError('No settings supplied')
    for key,value in changes.items():
        if key not in DEFAULTS:
            raise ValueError('Unknown setting: '+str(key))
        if key in BOOL_KEYS:
            if type(value) is not bool: raise ValueError(key+' must be boolean')
        elif type(value) is not int or value < 0 or value > 86400 * 30:
            raise ValueError(key+' must be a nonnegative integer within range')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for key,value in changes.items():
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
    return settings()


def peer_limits(db, peer):
    values={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}
    row=db.execute('SELECT limits_json FROM peers WHERE name=?',(peer,)).fetchone()
    if row: values.update(json.loads(row['limits_json']))
    return values


def set_peer(peer, *, blocked=None, disabled_until=None, rule_text=None, limits=None):
    if not peer or len(peer)>120: raise ValueError('Invalid peer name')
    if limits is not None:
        if not isinstance(limits,dict): raise ValueError('Invalid peer limits')
        for key,value in limits.items():
            if key not in DEFAULTS or key in BOOL_KEYS or type(value) is not int or value<0:
                raise ValueError('Invalid peer limit: '+str(key))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO peers(name) VALUES (?)',(peer,))
        if blocked is not None: db.execute('UPDATE peers SET blocked=? WHERE name=?',(int(bool(blocked)),peer))
        if disabled_until is not None: db.execute('UPDATE peers SET disabled_until=? WHERE name=?',(int(disabled_until),peer))
        if rule_text is not None: db.execute('UPDATE peers SET rule_text=? WHERE name=?',(str(rule_text)[:2000],peer))
        if limits is not None: db.execute('UPDATE peers SET limits_json=? WHERE name=?',(json.dumps(limits),peer))
        db.execute('INSERT INTO events(at,peer,kind,detail) VALUES (?,?,?,?)',
                   (int(time.time()),peer,'override','Administrative peer settings updated'))


def note_locked_files(peer,count):
    if not peer or not count:return
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO peers(name) VALUES (?)',(peer,))
        db.execute("UPDATE peers SET rule_text='Some files are peer-locked and cannot be requested' WHERE name=? AND rule_text=''",(peer,))
        db.execute('INSERT INTO events(at,peer,kind,detail) VALUES (?,?,?,?)',
                   (int(time.time()),peer,'peer_restriction',f'{count} locked search files excluded'))


def _eligibility(db, peer, file_count, bytes_queued, now):
    limits=peer_limits(db,peer)
    row=db.execute('SELECT * FROM peers WHERE name=?',(peer,)).fetchone()
    if limits['paused']: return 'Scheduler paused'
    if row and row['blocked']: return 'Peer explicitly blocked'
    if row and row['disabled_until']>now: return 'Peer temporarily disabled'
    if limits['fairness'] and row and row['last_completed']+limits['batch_cooldown_seconds']>now:
        return 'Peer cooldown active'
    if file_count>limits['queued_files_per_peer'] or bytes_queued>limits['queued_mb_per_peer']*1024*1024:
        return 'Batch exceeds peer queue limit'
    active=db.execute("SELECT COUNT(*),COALESCE(SUM(file_count),0),COALESCE(SUM(bytes_queued),0) FROM batches WHERE peer=? AND state IN ('enqueuing','queued','downloading')",(peer,)).fetchone()
    if active[0]>=limits['active_batches_per_peer'] or active[1]+file_count>limits['queued_files_per_peer'] or active[2]+bytes_queued>limits['queued_mb_per_peer']*1024*1024:
        return 'Peer queue limit reached'
    album_count=db.execute("SELECT COUNT(*) FROM batches WHERE peer=? AND file_count>1 AND state IN ('enqueuing','queued','downloading')",(peer,)).fetchone()[0]
    if file_count>1 and album_count>=limits['queued_albums_per_peer']:
        return 'Peer album queue limit reached'
    total=db.execute("SELECT COUNT(*) FROM batches WHERE state IN ('enqueuing','queued','downloading')").fetchone()[0]
    if total>=limits['global_concurrency']: return 'Global concurrency reached'
    daily=db.execute("SELECT COUNT(*) FROM batches WHERE peer=? AND created>=? AND state!='deferred'",(peer,now-86400)).fetchone()[0]
    if limits['fairness'] and daily>=limits['daily_batches_per_peer']: return 'Peer daily batch limit reached'
    return None


def eligibility(peer, file_count=1, bytes_queued=0):
    with connect() as db:
        return _eligibility(db,peer,file_count,bytes_queued,int(time.time()))


def reserve(task_id,peer,files):
    now=int(time.time()); size=sum(max(0,int(f['size'])) for f in files)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM batches WHERE task_id=?',(task_id,)).fetchone()
        if existing and existing['state'] in ('enqueuing','queued','downloading'):
            return False,'Existing or uncertain enqueue; reconcile before retry'
        native_id=task_id.split(':',1)[0]
        previous=[dict(r) for r in db.execute("SELECT state,updated,peer FROM batches WHERE task_id LIKE ? AND task_id!=? AND state!='deferred'",(native_id+':%',task_id))]
        limits=peer_limits(db,peer)
        reason=None
        if previous and not limits['auto_retries']:reason='Automatic retries disabled'
        elif len(previous)>=limits['retry_attempts']:reason='Retry attempt limit reached'
        elif previous and not limits['alternative_sources'] and any(p['peer'].casefold()!=peer.casefold() for p in previous):
            reason='Alternative sources disabled'
        elif previous and max(p['updated'] for p in previous)+limits['retry_backoff_seconds']>now:
            reason='Retry backoff active'
        reason=reason or _eligibility(db,peer,len(files),size,now)
        if reason:
            if not existing or existing['state']=='deferred':
                db.execute('''INSERT INTO batches(task_id,peer,state,files_json,file_count,bytes_queued,created,updated,next_check,reason)
                  VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                  state='deferred',updated=excluded.updated,next_check=excluded.next_check,
                  reason=excluded.reason''',
                  (task_id,peer,'deferred',json.dumps(files),len(files),size,now,now,
                   now+settings_value(db,'pending_check_seconds'),reason))
            db.execute('INSERT INTO events(at,peer,task_id,kind,detail) VALUES (?,?,?,?,?)',
                       (now,peer,task_id,'deferred',reason))
            return False,reason
        db.execute('INSERT OR IGNORE INTO peers(name) VALUES (?)',(peer,))
        db.execute('''INSERT INTO batches(task_id,peer,state,files_json,file_count,bytes_queued,created,updated,next_check,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            peer=excluded.peer,state=excluded.state,files_json=excluded.files_json,
            file_count=excluded.file_count,bytes_queued=excluded.bytes_queued,
            updated=excluded.updated,next_check=excluded.next_check,reason=excluded.reason,
            attempt=batches.attempt+1''',
            (task_id,peer,'enqueuing',json.dumps(files),len(files),size,now,now,now+300,'Submitting to slskd'))
        db.execute('INSERT INTO events(at,peer,task_id,kind,detail) VALUES (?,?,?,?,?)',
                   (now,peer,task_id,'enqueuing',f'{len(files)} files, {size} bytes'))
        return True,''


def mark(task_id,state,reason='',progress=None,queue_position=None,speed=None,bytes_moved=0,files_done=0):
    now=int(time.time())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM batches WHERE task_id=?',(task_id,)).fetchone()
        if not row: return
        if row['state']==state and progress is None: return
        advanced=max(0,bytes_moved)>row['last_bytes']
        db.execute('''UPDATE batches SET state=?,updated=?,reason=?,progress=COALESCE(?,progress),
          queue_position=?,speed=COALESCE(?,speed),next_check=?,last_bytes=MAX(last_bytes,?),
          progress_at=CASE WHEN ? THEN ? WHEN progress_at=0 THEN ? ELSE progress_at END
          WHERE task_id=?''',
          (state,now,reason,progress,queue_position,speed,
           now+settings_value(db,'pending_check_seconds'),max(0,bytes_moved),int(advanced),now,now,task_id))
        db.execute('UPDATE peers SET last_activity=? WHERE name=?',(now,row['peer']))
        if state in ('completed','failed','partial') and row['state'] not in ('completed','failed','partial'):
            db.execute('''UPDATE peers SET last_completed=?,success_count=success_count+?,
              failure_count=failure_count+?,files_downloaded=files_downloaded+?,
              bytes_transferred=bytes_transferred+?,speed_sum=speed_sum+?,
              speed_samples=speed_samples+? WHERE name=?''',
              (now,int(state=='completed'),int(state!='completed'),files_done,max(0,bytes_moved),
               max(0,speed or 0),int(bool(speed)),row['peer']))
        if row['state']!=state:
            db.execute('INSERT INTO events(at,peer,task_id,kind,detail) VALUES (?,?,?,?,?)',
                       (now,row['peer'],task_id,state,reason))


def settings_value(db,key):
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else DEFAULTS[key]


def is_stalled(task_id,bytes_moved):
    with connect() as db:
        row=db.execute('SELECT state,last_bytes,progress_at FROM batches WHERE task_id=?',(task_id,)).fetchone()
        threshold=settings_value(db,'stalled_seconds')
        return bool(row and threshold and row['state']=='downloading' and
                    bytes_moved<=row['last_bytes'] and row['progress_at'] and
                    time.time()-row['progress_at']>=threshold)


def cached_search(query):
    with connect() as db:
        row=db.execute('SELECT created,payload FROM search_cache WHERE query=?',(query,)).fetchone()
        ttl=max(settings_value(db,'candidate_cache_seconds'),
                settings_value(db,'duplicate_search_cooldown_seconds'))
        if row and row['created']+ttl>=time.time(): return json.loads(row['payload'])
    return None


def store_search(query,records):
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO search_cache VALUES (?,?,?)',
                   (query,int(time.time()),json.dumps(records)))
        db.execute('DELETE FROM search_cache WHERE created<?',(int(time.time())-86400,))


def reserve_search():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT next_at FROM search_gate WHERE id=1').fetchone()
        now=time.time()
        delay=max(0,row['next_at']-now)
        db.execute('UPDATE search_gate SET next_at=? WHERE id=1',
                   (max(now,row['next_at'])+settings_value(db,'search_interval_seconds'),))
        return delay


def snapshot():
    with connect() as db:
        peers=[dict(r) for r in db.execute('SELECT * FROM peers ORDER BY last_activity DESC')]
        batches=[dict(r) for r in db.execute('SELECT * FROM batches ORDER BY updated DESC LIMIT 500')]
        phases=[dict(r) for r in db.execute('SELECT * FROM acquisition_phases ORDER BY updated DESC LIMIT 500')]
    for peer in peers:
        peer['limits']=json.loads(peer.pop('limits_json'))
        peer['average_speed']=peer['speed_sum']/peer['speed_samples'] if peer['speed_samples'] else 0
    for batch in batches: batch['files']=json.loads(batch.pop('files_json'))
    for phase in phases:phase['alternatives']=json.loads(phase.pop('alternatives_json'))
    return {'peers':peers,'batches':batches,'phases':phases,'settings':settings()}


def note_phase(task_id,state,selected_peer='',alternatives=None,reason=''):
    now=int(time.time())
    with connect() as db:
        db.execute('''INSERT INTO acquisition_phases
          (task_id,state,selected_peer,alternatives_json,reason,created,updated)
          VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
          state=excluded.state,
          selected_peer=CASE WHEN excluded.selected_peer='' THEN acquisition_phases.selected_peer ELSE excluded.selected_peer END,
          alternatives_json=CASE WHEN ? THEN excluded.alternatives_json ELSE acquisition_phases.alternatives_json END,
          reason=excluded.reason,updated=excluded.updated''',
          (task_id,state,selected_peer,json.dumps(alternatives or []),reason,now,now,
           int(alternatives is not None)))
        if selected_peer:
            db.execute('INSERT OR IGNORE INTO peers(name) VALUES (?)',(selected_peer,))
            db.execute('INSERT INTO events(at,peer,task_id,kind,detail) VALUES (?,?,?,?,?)',
                       (now,selected_peer,task_id,state,reason))


def peer_history(peer):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events WHERE peer=? ORDER BY at DESC,id DESC LIMIT 500',(peer,))]


def rerank(candidates):
    """Use reliability/fairness only among candidates with matching safe evidence.

    Existing exact-version, acceptance-tier and quality ranking stays first.
    Only adjacent peers with the same tier, quality step, codec and rounded
    identity score can trade places; the original order breaks remaining ties.
    """
    if not candidates or not settings()['fairness']:
        return candidates
    with connect() as db:
        stats={r['name'].casefold():r for r in db.execute('SELECT * FROM peers')}
        active={r['peer'].casefold():r['n'] for r in db.execute("SELECT peer,COUNT(*) n FROM batches WHERE state IN ('enqueuing','queued','downloading') GROUP BY peer")}
    def band(candidate):
        decision=getattr(candidate,'quality_decision',None)
        step=getattr(decision,'preference_step',None)
        files=getattr(candidate,'files',[]) or []
        codec=tuple(sorted({getattr(f,'extension','').lower() for f in files}))
        return (candidate.tier,step,codec,round(candidate.final_score,2))
    def weight(candidate):
        row=stats.get(candidate.username.casefold())
        success=row['success_count'] if row else 0
        failure=row['failure_count'] if row else 0
        reliability=(success+1)/(success+failure+2)
        return (reliability,-active.get(candidate.username.casefold(),0))
    ranked=[]; index=0
    while index<len(candidates):
        end=index+1
        while end<len(candidates) and band(candidates[end])==band(candidates[index]):end+=1
        ranked.extend(sorted(candidates[index:end],key=weight,reverse=True))
        index=end
    return ranked
