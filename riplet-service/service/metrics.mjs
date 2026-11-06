import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';

export function createMetrics(dbPath) {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  db.exec(`PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
      format TEXT, quality TEXT, tier TEXT, duration_ms INTEGER,
      bytes INTEGER, delivered INTEGER, reason TEXT,
      referrer TEXT, country TEXT, device TEXT
    );
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);`);
  for (const [column,type] of [['referrer','TEXT'],['country','TEXT'],['device','TEXT']]) {
    try { db.exec(`ALTER TABLE events ADD COLUMN ${column} ${type}`); } catch (error) { if (!/duplicate column/i.test(error.message)) throw error; }
  }
  const insert = db.prepare('INSERT INTO events (ts,kind,format,quality,tier,duration_ms,bytes,delivered,reason,referrer,country,device) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)');
  const count = db.prepare('SELECT kind,COUNT(*) AS count,SUM(COALESCE(bytes,0)) AS bytes,SUM(COALESCE(delivered,0)) AS delivered FROM events WHERE ts>=? GROUP BY kind');
  const trend = db.prepare('SELECT (ts / 3600000) * 3600000 AS hour,kind,COUNT(*) AS count FROM events WHERE ts>=? GROUP BY hour,kind ORDER BY hour');
  const breakdown = db.prepare("SELECT format,quality,COUNT(*) AS count FROM events WHERE ts>=? AND kind='job_started' GROUP BY format,quality ORDER BY count DESC");
  const errors = db.prepare("SELECT reason,COUNT(*) AS count FROM events WHERE ts>=? AND reason IS NOT NULL GROUP BY reason ORDER BY count DESC LIMIT 15");
  const sizes = db.prepare("SELECT COUNT(*) AS files,SUM(COALESCE(bytes,0)) AS output_bytes,AVG(duration_ms) AS avg_duration_ms FROM events WHERE ts>=? AND kind='job_completed'");
  const visitors = db.prepare("SELECT country,device,referrer,COUNT(*) AS count FROM events WHERE ts>=? AND kind='pageview' GROUP BY country,device,referrer ORDER BY count DESC LIMIT 30");
  function record(kind, data={}) {
    insert.run(Date.now(),kind,data.format ?? null,data.quality ?? null,data.tier ?? null,
      data.durationMs ?? null,data.bytes ?? null,data.delivered ?? null,data.reason ?? null,
      data.referrer ?? null,data.country ?? null,data.device ?? null);
  }
  function snapshot(hours=24) {
    const since=Date.now()-hours*3600000;
    return { hours, counts:count.all(since),trend:trend.all(since),breakdown:breakdown.all(since),errors:errors.all(since),visitors:visitors.all(since),sizes:sizes.get(since) };
  }
  function prune(days=31) { db.prepare('DELETE FROM events WHERE ts<?').run(Date.now()-days*86400000); }
  return { record,snapshot,prune,close:()=>db.close() };
}
