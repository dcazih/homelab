"""Event-only sync service: private authenticated enqueue + bounded Navidrome reconciliation."""
import asyncio, json, os, hmac, sqlite3, time, logging, hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import httpx

log = logging.getLogger("cassette_events")
DB = os.environ.get("EVENTS_DB", "/state/events.db")
PROFILES_FILE = os.environ.get("PROFILES_FILE", "/state/profiles.json")
BRIDGE = os.environ.get("BRIDGE_URL", "http://music-profiles:8765").rstrip("/")
SECRET = os.environ.get("EVENT_SYNC_SECRET", "")
if len(SECRET) < 32:
    raise RuntimeError("EVENT_SYNC_SECRET must be at least 32 characters")


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, event_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER DEFAULT 0, next_due INTEGER NOT NULL, created INTEGER NOT NULL, error TEXT DEFAULT '')")
    c.commit()
    return c


def profiles():
    p = Path(PROFILES_FILE)
    if not p.exists(): return {}
    entries = json.loads(p.read_text())
    if not isinstance(entries, dict): raise ValueError("profiles must be a JSON object")
    return {str(k): str(v) for k, v in entries.items() if isinstance(v, str) and len(v) >= 24}


class Event(BaseModel):
    kind: str
    release_group_mbid: str = Field(default="", max_length=100)
    causation_id: str = Field(default="", max_length=128)


async def do_reconcile():
    creds = profiles()
    if not creds:
        raise RuntimeError("No Bridge profile credentials configured")
    outcomes = []
    async with httpx.AsyncClient(timeout=25) as client:
        for name, token in creds.items():
            head = {"Authorization": f"Bearer {token}"}
            for path in ("/v1/requests/reconcile", "/v1/reconcile"):
                try:
                    result = await client.post(BRIDGE + path, headers=head)
                    if result.status_code in (404, 405):
                        continue
                    if result.status_code >= 400:
                        raise RuntimeError(f"{path} HTTP {result.status_code} for {name}")
                    outcomes.append((name, path, result.status_code))
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"Bridge not reachable on {path}: {type(exc).__name__}") from exc
    return outcomes


async def work_loop():
    while True:
        with db() as c:
            item = c.execute("SELECT id, attempts FROM events WHERE state='pending' AND next_due<=? ORDER BY created LIMIT 1", (int(time.time()),)).fetchone()
            if item:
                c.execute("UPDATE events SET state='running' WHERE id=?", (item[0],))
                c.commit()
        if not item:
            await asyncio.sleep(1)
            continue
        eid, attempts = item
        try:
            result = await do_reconcile()
            log.info("Reconciliation triggered: %d endpoint calls", len(result))
            # After import_finished, do limited followups because Navidrome indexing may lag.
            # No periodic work occurs in the absence of a completed-import event.
            followup = [10, 25, 60, 120, 240]
            if attempts < len(followup):
                with db() as c:
                    c.execute("UPDATE events SET state='pending', attempts=?, next_due=?, error='' WHERE id=?", (attempts+1, int(time.time())+followup[attempts], eid))
                    c.commit()
            else:
                with db() as c:
                    c.execute("UPDATE events SET state='done', attempts=?, error='' WHERE id=?", (attempts+1,eid))
                    c.commit()
        except Exception as exc:
            log.warning("Sync event failed: %s", str(exc)[:200])
            if attempts >= 12:
                with db() as c:
                    c.execute("UPDATE events SET state='failed', attempts=?, error=? WHERE id=?", (attempts+1,str(exc)[:200],eid))
                    c.commit()
            else:
                delay = min(600, 15 * (2 ** min(attempts, 5)))
                with db() as c:
                    c.execute("UPDATE events SET state='pending', attempts=?, next_due=?, error=? WHERE id=?", (attempts+1,int(time.time())+delay,str(exc)[:200],eid))
                    c.commit()


@asynccontextmanager
async def lifespan(app):
    with db() as c:
        c.execute("UPDATE events SET state='pending' WHERE state='running'")
        c.commit()
    task = asyncio.create_task(work_loop())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    with db() as c:
        counts = dict(c.execute("SELECT state, COUNT(*) FROM events GROUP BY state").fetchall())
    return {"ok": True, "profiles_configured": len(profiles()), "events": counts}


@app.post("/event", status_code=202)
def event(ev: Event, x_event_secret: str = Header(default="")):
    if not hmac.compare_digest(x_event_secret, SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if ev.kind != "import_finished":
        raise HTTPException(status_code=400, detail="Only import_finished events are supported")
    now = int(time.time())
    key = ev.causation_id or hashlib.sha256(f"{ev.kind}:{ev.release_group_mbid}:{now//20}".encode()).hexdigest()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO events (id,event_json,state,attempts,next_due,created) VALUES (?,?, 'pending',0,?,?)", (key, ev.model_dump_json(),now,now))
        c.commit()
    return {"accepted": True, "event_id": key}
