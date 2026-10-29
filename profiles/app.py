"""Music Profiles Bridge: shared Navidrome catalog, user-owned playlists & approved proposals.
Prototype for trusted household / tailnet. Does not scrape streaming providers.
"""
import os, re, sqlite3, secrets, hashlib, hmac, time, json, uuid, asyncio, logging, unicodedata
from contextlib import contextmanager
from typing import Optional, Literal
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dn_accounts import (initialize as init_dn_accounts, linked_metadata, linked_token,
    save_link, unlink as unlink_dn, authenticate as dn_authenticate, identity as dn_identity,
    companion_session, revoke_companion, logout_temporary_session)

NAV = os.environ.get('NAVIDROME_URL','http://host.docker.internal:4533').rstrip('/')
DN = os.environ.get('DROPPEDNEEDLE_URL','http://droppedneedle:8688').rstrip('/')
# A global bearer is intentionally ignored. Requests MUST use the linked person's token.
LOG = logging.getLogger('music_profiles')
DB = os.environ.get('PROFILE_DB','/data/profiles.db')
ADMIN = os.environ.get('PROFILE_ADMIN_KEY','')
FERNET = os.environ.get('PROFILE_ENCRYPTION_KEY','')
CASSETTE_INTERNAL_KEY = os.environ.get('CASSETTE_BRIDGE_INTERNAL_KEY','')
if len(ADMIN)<32 or not FERNET:
    raise RuntimeError('Configure PROFILE_ADMIN_KEY (32+ characters) and PROFILE_ENCRYPTION_KEY')
cipher = Fernet(FERNET.encode())
app = FastAPI(title='Music Profiles Bridge', version='0.1.0', description='Separate user playlists over a single shared Navidrome collection. Proposals require user approval. No music provider passwords are collected.')
app.mount('/assets', StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'assets')), name='assets')

@contextmanager
def connection():
    db=sqlite3.connect(DB,timeout=15)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    try:
        yield db
        db.commit()
    finally:
        db.close()

def init():
    os.makedirs(os.path.dirname(DB),exist_ok=True)
    with connection() as d:
        d.execute('PRAGMA journal_mode=WAL')
        d.executescript('''
        CREATE TABLE IF NOT EXISTS invites (digest TEXT PRIMARY KEY, expires INT NOT NULL, used INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
            secret BLOB NOT NULL, token_digest TEXT UNIQUE NOT NULL, created INT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile_sessions (token_digest TEXT PRIMARY KEY, user_id TEXT NOT NULL, created INT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile_layouts (owner TEXT PRIMARY KEY, layout_json TEXT NOT NULL, updated INT NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, artist TEXT NOT NULL,
            title TEXT NOT NULL, album TEXT, recording_mbid TEXT, source TEXT, state TEXT NOT NULL,
            nav_song_id TEXT, message TEXT, created INT NOT NULL,
            UNIQUE(user_id, artist, title, album));
        ''')
        # Stage 4.3: hide individual proposals without deleting their underlying
        # request/reconciliation record. Existing databases are migrated in place.
        columns = {col['name'] for col in d.execute('PRAGMA table_info(proposals)')}
        if 'dismissed_at' not in columns:
            d.execute('ALTER TABLE proposals ADD COLUMN dismissed_at INTEGER')
        d.execute("UPDATE proposals SET state='submission_uncertain', message='Bridge restarted during submission; inspect DroppedNeedle before retrying' WHERE state='submitting'")
init()
init_dn_accounts(connection)
from shared_acquisition import initialize as init_shared_acquisition
init_shared_acquisition(connection)

def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def normalize(s): return re.sub(r'[^\w]+',' ',(s or '').casefold()).strip()
def err(msg,status=400): raise HTTPException(status,msg)

class InviteOut(BaseModel):
    ttl_hours:int=Field(default=72,ge=1,le=720)
class ConnectIn(BaseModel):
    invite_code:str
    navidrome_username:str=Field(min_length=1,max_length=80)
    navidrome_password:str=Field(min_length=1,max_length=256)
class ReconnectIn(BaseModel):
    navidrome_username:str=Field(min_length=1,max_length=80)
    navidrome_password:str=Field(min_length=1,max_length=256)
class LoginIn(BaseModel):
    navidrome_username:str=Field(min_length=1,max_length=80)
    navidrome_password:str=Field(min_length=1,max_length=256)
class AdminLoginIn(BaseModel):
    key:str=Field(min_length=1)
class AdminPasswordResetIn(BaseModel):
    new_password:str=Field(min_length=8,max_length=256)
class LayoutIn(BaseModel):
    order:list[str]=Field(max_length=40)
    positions:dict[str,list[int]]=Field(default_factory=dict)
class ClearRequestsIn(BaseModel):
    album_ids:Optional[list[str]]=None

class ProposedSong(BaseModel):
    artist:str=Field(min_length=1,max_length=160)
    title:str=Field(min_length=1,max_length=200)
    album:str=Field(default='',max_length=200)
    recording_mbid:Optional[str]=Field(default=None,max_length=36)
    source:str=Field(default='user suggestion',max_length=100)
class ProposalBatch(BaseModel):
    songs:list[ProposedSong]=Field(min_length=1,max_length=100)
class SelectSong(BaseModel):
    navidrome_song_id:str=Field(min_length=1,max_length=128)

def require_admin(x_admin_key:str=Header(default='')):
    if not hmac.compare_digest(x_admin_key,ADMIN): err('Invalid admin credential',401)

def require_user(authorization:str=Header(default='')):
    if not authorization.startswith('Bearer '): err('Bearer token required',401)
    token=authorization[7:].strip()
    with connection() as d:
        row=d.execute('''SELECT users.* FROM users WHERE users.token_digest=?
                         OR users.id=(SELECT user_id FROM profile_sessions WHERE token_digest=?)''',
                      (digest(token),digest(token))).fetchone()
    if not row: err('Invalid token',401)
    return dict(row)

async def sub(user,endpoint,params=None):
    pwd=cipher.decrypt(user['secret']).decode()
    salt=secrets.token_hex(12)
    auth={'u':user['username'],'t':hashlib.md5((pwd+salt).encode()).hexdigest(),'s':salt,'v':'1.16.1','c':'music-profiles-bridge','f':'json'}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res=await client.post(f'{NAV}/rest/{endpoint}.view',data={**auth,**(params or {})})
            res.raise_for_status()
            data=res.json().get('subsonic-response',{})
    except (httpx.HTTPError,ValueError) as e:
        err('Navidrome unreachable or returned invalid response',502)
    if data.get('status')!='ok':
        err('Navidrome: '+str(data.get('error',{}).get('message','unknown response')),502)
    return data

def personal_playlist_name(username):
    return f"{username}’s Music"


async def ensure_playlist(user):
    """Migrate the owned legacy playlist in place, preserving its ID and contents.

    An ambiguous pair of old/new playlists is never combined or overwritten.
    """
    name = personal_playlist_name(user['username'])
    data = await sub(user, 'getPlaylists')
    pls = data.get('playlists', {}).get('playlist', []) or []
    if isinstance(pls, dict): pls = [pls]
    mine = [p for p in pls if p.get('owner','').casefold() == user['username'].casefold()
            and p.get('name','').casefold() in ('my music', name.casefold())]
    if len(mine) > 1:
        err('Two owned music playlists exist; resolve the duplicate before proceeding', 409)
    if mine:
        playlist_id = str(mine[0]['id'])
        if mine[0]['name'].casefold() != name.casefold():
            await sub(user, 'updatePlaylist', {'playlistId': playlist_id, 'name': name, 'public': 'false'})
        return playlist_id
    created = await sub(user, 'createPlaylist', {'name': name})
    playlist_id = (created.get('playlist') or {}).get('id')
    if not playlist_id:
        again = await sub(user, 'getPlaylists')
        entries = again.get('playlists',{}).get('playlist',[]) or []
        if isinstance(entries,dict): entries=[entries]
        matches = [p for p in entries if p.get('name','').casefold() == name.casefold()
                   and p.get('owner','').casefold() == user['username'].casefold()]
        if len(matches) == 1: playlist_id = matches[0]['id']
    if not playlist_id: err('Unable to locate newly-created personal playlist', 502)
    await sub(user, 'updatePlaylist', {'playlistId':str(playlist_id), 'public':'false'})
    return str(playlist_id)

async def playlist_ids(user):
    pid=await ensure_playlist(user)
    detail=await sub(user,'getPlaylist',{'id':pid})
    songs=detail.get('playlist',{}).get('entry',[]) or []
    if isinstance(songs,dict): songs=[songs]
    return pid,set(str(s['id']) for s in songs)

async def search(user,artist,title):
    query=f'{artist} {title}'.strip()
    data=await sub(user,'search3',{'query':query,'songCount':'100','albumCount':'0','artistCount':'0'})
    songs=data.get('searchResult3',{}).get('song',[]) or []
    if isinstance(songs,dict): songs=[songs]
    exact=[s for s in songs if normalize(s.get('artist'))==normalize(artist) and normalize(s.get('title'))==normalize(title)]
    return exact,songs[:30]


# A DroppedNeedle import can rename files, use a different album edition, or
# normalize multi-artist credits. Searching "artist + title" alone is unreliable.
def _fold(value):
    text=unicodedata.normalize('NFKD',str(value or '').casefold())
    return ' '.join(re.findall('[a-z0-9]+',''.join(c for c in text if not unicodedata.combining(c))))


def _proposal_title(proposal):
    title=str(proposal.get('title') or '')
    album=_fold(proposal.get('album'))
    # Some catalog entries append the album in parentheses to the title.
    title=re.sub(r'\s*\(([^()]*)\)\s*$',lambda m: '' if _fold(m.group(1))==album and album else m.group(0),title)
    return _fold(title)


def _match_score(proposal,song):
    """Only admit strong matches; never match based on title alone."""
    wanted_id=str(proposal.get('recording_mbid') or '').casefold()
    found_id=str(song.get('musicBrainzId') or '').casefold()
    if wanted_id and found_id and wanted_id!=found_id:
        return -1  # Two distinct verified recording IDs must never be conflated.
    if wanted_id and found_id==wanted_id:return 1000
    title=_fold(song.get('title'))
    wanted_title=_proposal_title(proposal)
    if title!=wanted_title or not title:return -1
    artist=_fold(song.get('artist'))
    requested_artist=_fold(proposal.get('artist'))
    album=_fold(song.get('album'))
    requested_album=_fold(proposal.get('album'))
    if requested_artist and artist==requested_artist:
        artist_score=80
    elif len(artist)>=5 and artist in requested_artist:
        artist_score=65
    elif len(requested_artist)>=5 and requested_artist in artist:
        artist_score=60
    else:
        ignored={'the','feat','featuring','and','with','vs','jay','a','j'}
        left={w for w in artist.split() if len(w)>2 and w not in ignored}
        right={w for w in requested_artist.split() if len(w)>2 and w not in ignored}
        shared=left & right
        artist_score=42 if len(shared)>=2 else 0
    album_score=70 if requested_album and requested_album==album else 0
    if artist_score==0 and album_score==0:return -1
    return 100+artist_score+album_score


async def find_proposal_matches(user,proposal):
    """Return confident tracks, ranked. Prefer MusicBrainz IDs, then release metadata.

    A second query handles older Navidrome search behavior that requires both
    title and artist words even when a title-only query returned no songs.
    """
    title=proposal.get('title') or ''
    queries=[title]
    combined=f"{proposal.get('artist','')} {title}".strip()
    if combined!=title:queries.append(combined)
    candidates=[]
    seen=set()
    for query in queries:
        data=await sub(user,'search3',{'query':query,'songCount':'100','albumCount':'0','artistCount':'0'})
        songs=data.get('searchResult3',{}).get('song',[]) or []
        if isinstance(songs,dict):songs=[songs]
        for song in songs:
            if not isinstance(song,dict) or not song.get('id'):continue
            song_id=str(song['id'])
            if song_id not in seen:
                seen.add(song_id)
                candidates.append(song)
        if candidates and any(_match_score(proposal,x)>=140 for x in candidates):break
    ranked=sorted(((_match_score(proposal,x),x) for x in candidates),
                  key=lambda pair:pair[0],reverse=True)
    credible=[(score,song) for score,song in ranked if score>=140]
    if not credible:return [],candidates[:30]
    # Identical recordings may exist in several encodings/editions. Never
    # choose one arbitrarily when multiple equally good files were imported.
    if len(credible)>1 and credible[0][0]-credible[1][0]<35:
        return [s for _,s in credible],candidates[:30]
    return [credible[0][1]],candidates[:30]

async def append_to_my_music(user,song_id):
    # Explicit authorization by authenticated user, and server verifies this ID is available.
    song_data=await sub(user,'getSong',{'id':song_id})
    song=song_data.get('song',{})
    if not song or str(song.get('id'))!=str(song_id): err('Unknown song ID',404)
    pid,already=await playlist_ids(user)
    if str(song_id) not in already:
        await sub(user,'updatePlaylist',{'playlistId':pid,'songIdToAdd':song_id})
    return song, str(song_id) not in already

class DroppedNeedleLogin(BaseModel):
    username:str=Field(min_length=1,max_length=120)
    password:str=Field(min_length=1,max_length=512)


class DroppedNeedleSession(BaseModel):
    # For users with SSO/other auth: paste an existing STANDARD session token once.
    standard_token:str=Field(min_length=8,max_length=4096)


def assert_dn_account_unclaimed(profile_id, dn_user_id):
    """Check uniqueness BEFORE minting; DN replaces same-label sessions on mint."""
    with connection() as db:
        previous=db.execute('SELECT profile_id FROM droppedneedle_accounts WHERE dn_user_id=?',
                            (dn_user_id,)).fetchone()
    if previous and previous['profile_id']!=profile_id:
        err('That DroppedNeedle account is already linked to another family profile',409)


async def link_dn_for_user(user, standard_token):
    """Verify account ownership before minting a device session."""
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        account=await dn_identity(client, DN, standard_token)
        assert_dn_account_unclaimed(user['id'],account['dn_user_id'])
        session = await companion_session(client, DN, standard_token)
        verified = await dn_identity(client, DN, session)
    if verified['dn_user_id'] != account['dn_user_id']:
        err('DroppedNeedle returned a different account for the companion session',502)
    save_link(connection, cipher, user['id'], verified['dn_user_id'], verified['dn_username'], session)
    return {'linked':True, 'username':verified['dn_username'],
            'message':'Linked. Your separate DroppedNeedle companion session lasts up to 30 days.'}


@app.get('/v1/me/droppedneedle')
def my_dn_status(user=Depends(require_user)):
    record=linked_metadata(connection,user['id'])
    if not record:
        return {'linked':False, 'status':'not_linked'}
    return {'linked':True, 'status':'linked_not_recently_verified',
            'username':record['dn_username'], 'linked_at':record['linked_at']}


@app.post('/v1/me/droppedneedle/connect')
async def my_dn_connect(data:DroppedNeedleLogin, user=Depends(require_user)):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        standard_token=await dn_authenticate(client,DN,data.username,data.password)
        try:
            account=await dn_identity(client,DN,standard_token)
            assert_dn_account_unclaimed(user['id'],account['dn_user_id'])
            companion=await companion_session(client,DN,standard_token)
            verified=await dn_identity(client,DN,companion)
        finally:
            # Login creates a regular DN session; the Bridge only needs its companion.
            await logout_temporary_session(client,DN,standard_token)
    if verified['dn_user_id']!=account['dn_user_id']:
        err('DroppedNeedle returned a different account for the companion session',502)
    save_link(connection,cipher,user['id'],verified['dn_user_id'],verified['dn_username'],companion)
    return {'linked':True,'username':verified['dn_username'],
            'message':'Connected. Reconnect when the 30-day companion session expires.'}


@app.post('/v1/me/droppedneedle/connect-session')
async def my_dn_connect_session(data:DroppedNeedleSession, user=Depends(require_user)):
    return await link_dn_for_user(user,data.standard_token)


@app.post('/v1/me/droppedneedle/verify')
async def my_dn_verify(user=Depends(require_user)):
    token=linked_token(connection,cipher,user['id'])
    if not token: return {'linked':False,'status':'not_linked'}
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            account=await dn_identity(client,DN,token)
    except HTTPException as ex:
        if ex.status_code==401:
            return {'linked':True,'status':'expired_or_revoked','reconnect_required':True}
        raise
    record=linked_metadata(connection,user['id'])
    if account['dn_user_id'] != record['dn_user_id']:
        return {'linked':True,'status':'identity_mismatch','reconnect_required':True}
    return {'linked':True,'status':'connected','username':account['dn_username'],
            'reconnect_required':False}


@app.delete('/v1/me/droppedneedle')
async def my_dn_unlink(user=Depends(require_user)):
    token=linked_token(connection,cipher,user['id'])
    unlink_dn(connection,user['id'])
    if token:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            await revoke_companion(client,DN,token)
    return {'linked':False, 'status':'disconnected'}


class CassetteRequest(BaseModel):
    username:str=Field(min_length=1,max_length=80)
    artist:str=Field(min_length=1,max_length=160)
    title:str=Field(min_length=1,max_length=200)
    album:str=Field(default='',max_length=200)
    recording_mbid:str=Field(min_length=36,max_length=36)


def require_cassette_internal(x_cassette_internal_key:str=Header(default='')):
    if (len(CASSETTE_INTERNAL_KEY)<32 or
            not hmac.compare_digest(x_cassette_internal_key,CASSETTE_INTERNAL_KEY)):
        err('Not authorized',401)


@app.post('/v1/internal/cassette/request',dependencies=[Depends(require_cassette_internal)],include_in_schema=False)
async def cassette_request(req:CassetteRequest):
    """Trusted proxy only. Proxy must validate user's Navidrome credentials
    before calling; bridge maps that identity to its enrolled profile.
    Never accepts arbitrary DroppedNeedle URLs or arbitrary download targets.
    """
    if not re.fullmatch(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}',req.recording_mbid):
        err('Recording MBID is invalid',400)
    with connection() as d:
        row=d.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(req.username,)).fetchone()
    if not row:
        err('This Navidrome user must first connect a Music Bridge profile',404)
    user=dict(row)
    with connection() as d:
        prop=d.execute('SELECT id,state,nav_song_id FROM proposals WHERE user_id=? AND artist=? AND title=? AND album=?',
                       (user['id'],req.artist,req.title,req.album)).fetchone()
        if prop and prop['state'] in ('requested','added','submitting','submission_uncertain'):
            # A subsequent star is an explicit new interaction: show it again.
            d.execute('UPDATE proposals SET dismissed_at=NULL WHERE id=? AND user_id=?',
                      (prop['id'],user['id']))
            return {'status':prop['state'],'song_id':prop['nav_song_id'],'proposal_id':prop['id']}
        if prop:
            pid=prop['id']
            d.execute('UPDATE proposals SET recording_mbid=?,source=?,state=?,dismissed_at=NULL WHERE id=? AND user_id=?',
                      (req.recording_mbid,'Cassette star','proposed',pid,user['id']))
        else:
            pid=str(uuid.uuid4())
            d.execute('INSERT INTO proposals(id,user_id,artist,title,album,recording_mbid,source,state,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid,user['id'],req.artist,req.title,req.album,req.recording_mbid,'Cassette star','proposed',int(time.time())))
    result=await approve(pid,user=user)
    return {**result,'proposal_id':pid}


class CassetteCatalogQuery(BaseModel):
    username:str=Field(min_length=1,max_length=80)
    query:str=Field(min_length=2,max_length=160)
    limit:int=Field(default=10,ge=1,le=30)

class CassetteAlbumTracks(BaseModel):
    username:str=Field(min_length=1,max_length=80)
    album_mbid:str=Field(min_length=36,max_length=36)

async def dn_catalog_for_user(username, path, params=None):
    # Proxy already validated Navidrome auth, and this route requires internal key.
    with connection() as db:
        row=db.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(username,)).fetchone()
    if not row:
        return {}
    token=linked_token(connection,cipher,row['id'])
    if not token:
        return {}
    try:
        async with httpx.AsyncClient(timeout=12,trust_env=False,follow_redirects=False) as client:
            res=await client.get(DN+path,params=params,
                headers={'Authorization':f'Bearer {token}'})
            if res.status_code in (401,403):return {}
            res.raise_for_status()
            data=res.json()
            return data if isinstance(data,dict) else {}
    except (httpx.HTTPError,ValueError):
        LOG.warning('User-scoped DroppedNeedle catalog request unavailable')
        return {}

@app.post('/v1/internal/cassette/dn/search',
          dependencies=[Depends(require_cassette_internal)],include_in_schema=False)
async def cassette_dn_catalog(body:CassetteCatalogQuery):
    return await dn_catalog_for_user(body.username,'/api/v1/search',
        {'q':body.query,'limit_artists':body.limit,'limit_albums':body.limit})

@app.post('/v1/internal/cassette/dn/search/albums',
          dependencies=[Depends(require_cassette_internal)],include_in_schema=False)
async def cassette_dn_albums(body:CassetteCatalogQuery):
    return await dn_catalog_for_user(body.username,'/api/v1/search/albums',
                                      {'q':body.query,'limit':body.limit})

@app.post('/v1/internal/cassette/dn/album-tracks',
          dependencies=[Depends(require_cassette_internal)],include_in_schema=False)
async def cassette_dn_tracks(body:CassetteAlbumTracks):
    if not re.fullmatch(r'[a-fA-F0-9-]{36}',body.album_mbid):err('Invalid album MBID',400)
    return await dn_catalog_for_user(body.username,
                   f'/api/v1/albums/{quote(body.album_mbid)}/tracks')


@app.get('/health')
def health(): return {'status':'ok'}

@app.post('/v1/admin/invites', dependencies=[Depends(require_admin)])
def invite(req:InviteOut):
    code=secrets.token_urlsafe(24)
    with connection() as d:
        d.execute('INSERT INTO invites(digest,expires) VALUES(?,?)',(digest(code),int(time.time())+req.ttl_hours*3600))
    return {'invite_code':code,'expires_in_hours':req.ttl_hours}

@app.post('/v1/connect')
async def connect(req:ConnectIn):
    with connection() as d:
        row=d.execute('SELECT * FROM invites WHERE digest=?',(digest(req.invite_code),)).fetchone()
        if not row or row['used'] or row['expires']<time.time(): err('Invite invalid, expired, or used',401)
        if d.execute('SELECT 1 FROM users WHERE username=?',(req.navidrome_username,)).fetchone(): err('User already enrolled',409)
    user={'username':req.navidrome_username,'secret':cipher.encrypt(req.navidrome_password.encode())}
    await sub(user,'ping')
    await ensure_playlist(user)
    token=secrets.token_urlsafe(32)
    with connection() as d:
        # Atomic consume: concurrent invite usage cannot create two accounts.
        changed=d.execute('UPDATE invites SET used=1 WHERE digest=? AND used=0 AND expires>=?',(digest(req.invite_code),int(time.time())))
        if changed.rowcount!=1: err('Invite already used',409)
        d.execute('INSERT INTO users VALUES(?,?,?,?,?)',(str(uuid.uuid4()),req.navidrome_username,user['secret'],digest(token),int(time.time())))
    return {'username':req.navidrome_username,'profile_token':token,'message':'Keep this token private. It is shown once.'}

@app.post('/v1/auth/login')
async def login(req:LoginIn):
    with connection() as d:
        row=d.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(req.navidrome_username,)).fetchone()
    if not row: err('This Navidrome account has not joined your library',401)
    candidate={'username':row['username'],'secret':cipher.encrypt(req.navidrome_password.encode())}
    try: await sub(candidate,'ping')
    except HTTPException as exc:
        if exc.status_code==502 and str(exc.detail).startswith('Navidrome:'): err('Navidrome sign-in failed',401)
        raise
    token=secrets.token_urlsafe(32)
    with connection() as d:
        d.execute('UPDATE users SET secret=? WHERE id=?',(candidate['secret'],row['id']))
        d.execute('INSERT INTO profile_sessions VALUES(?,?,?)',(digest(token),row['id'],int(time.time())))
    return {'profile_token':token,'username':row['username']}

@app.post('/v1/auth/logout')
def logout_user(authorization:str=Header(default=''),user=Depends(require_user)):
    with connection() as d:
        token_hash=digest(authorization[7:].strip())
        d.execute('DELETE FROM profile_sessions WHERE user_id=? AND token_digest=?',(user['id'],token_hash))
        d.execute('UPDATE users SET token_digest=? WHERE id=? AND token_digest=?',(digest(secrets.token_urlsafe(32)),user['id'],token_hash))
    return {'status':'logged_out'}

@app.post('/v1/admin/login')
def admin_login(body:AdminLoginIn):
    if not hmac.compare_digest(body.key,ADMIN): err('Invalid admin credential',401)
    return {'status':'ok'}

@app.get('/v1/admin/accounts',dependencies=[Depends(require_admin)])
def admin_accounts():
    with connection() as d:
        rows=d.execute('SELECT id,username,created FROM users ORDER BY username COLLATE NOCASE').fetchall()
    return {'accounts':[dict(row) for row in rows]}

@app.post('/v1/admin/accounts/{account_id}/reset-session',dependencies=[Depends(require_admin)])
def admin_reset_session(account_id:str):
    with connection() as d:
        changed=d.execute('UPDATE users SET token_digest=? WHERE id=?',(digest(secrets.token_urlsafe(32)),account_id))
        d.execute('DELETE FROM profile_sessions WHERE user_id=?',(account_id,))
    if changed.rowcount!=1: err('Account not found',404)
    return {'status':'reset','message':'Bridge session revoked. The user can sign in again with Navidrome.'}

@app.post('/v1/admin/accounts/{account_id}/reset-password',dependencies=[Depends(require_admin)])
async def admin_reset_password(account_id:str,body:AdminPasswordResetIn,admin_user=Depends(require_user)):
    with connection() as d:
        target=d.execute('SELECT id,username FROM users WHERE id=?',(account_id,)).fetchone()
    if not target: err('Account not found',404)
    # Subsonic's user-management methods are not implemented by Navidrome.
    # Use the native API, preserving the entire existing user record in the PUT.
    password=cipher.decrypt(admin_user['secret']).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            login=await client.post(f'{NAV}/auth/login',json={'username':admin_user['username'],'password':password})
            login.raise_for_status()
            auth=login.json()
            if not auth.get('isAdmin') or not auth.get('token'):
                err('Your signed-in Navidrome account must be an administrator',403)
            headers={'x-nd-authorization':'Bearer '+auth['token']}
            listing=await client.get(f'{NAV}/api/user',headers=headers,params={'_start':0,'_end':10000})
            listing.raise_for_status()
            matches=[u for u in listing.json() if u.get('userName','').casefold()==target['username'].casefold()]
            if len(matches)!=1: err('Navidrome account could not be uniquely identified',404)
            record=matches[0]
            record['password']=body.new_password
            if record['id']==auth.get('id'): record['currentPassword']=password
            changed=await client.put(f"{NAV}/api/user/{quote(str(record['id']),safe='')}",headers=headers,json=record)
            changed.raise_for_status()
    except (httpx.HTTPError, ValueError, KeyError):
        err('Navidrome could not reset this password; check the signed-in account and server',502)
    encrypted=cipher.encrypt(body.new_password.encode())
    with connection() as d:
        d.execute('UPDATE users SET secret=?,token_digest=? WHERE id=?',
                  (encrypted,digest(secrets.token_urlsafe(32)),account_id))
        d.execute('DELETE FROM profile_sessions WHERE user_id=?',(account_id,))
    return {'status':'reset','message':'Navidrome password updated and Bridge sessions revoked.'}

@app.get('/v1/me/layout')
def get_personal_layout(user=Depends(require_user)):
    return _get_layout('user:'+user['id'])

@app.put('/v1/me/layout')
def put_personal_layout(body:LayoutIn,user=Depends(require_user)):
    return _put_layout('user:'+user['id'],body)

@app.get('/v1/admin/layout',dependencies=[Depends(require_admin)])
def get_admin_layout():
    return _get_layout('admin')

@app.put('/v1/admin/layout',dependencies=[Depends(require_admin)])
def put_admin_layout(body:LayoutIn):
    return _put_layout('admin',body)

def _get_layout(owner):
    with connection() as d:
        row=d.execute('SELECT layout_json FROM profile_layouts WHERE owner=?',(owner,)).fetchone()
    saved=json.loads(row['layout_json']) if row else {'order':[],'positions':{}}
    return saved if isinstance(saved,dict) else {'order':saved,'positions':{}}

def _put_layout(owner,body):
    allowed={'upload','pending','peer-history','scheduler-settings','active-scheduler','output-island'}
    order=list(dict.fromkeys(body.order))
    if any(item not in allowed for item in order): err('Invalid island name')
    positions={name:point for name,point in body.positions.items()
               if name=='output-island' and len(point)==2 and all(isinstance(n,int) and 0<=n<=10000 for n in point)}
    saved={'order':order,'positions':positions}
    with connection() as d:
        d.execute('INSERT INTO profile_layouts(owner,layout_json,updated) VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET layout_json=excluded.layout_json,updated=excluded.updated',(owner,json.dumps(saved),int(time.time())))
    return saved

@app.get('/v1/me')
async def me(user=Depends(require_user)):
    pid,ids=await playlist_ids(user)
    return {'username':user['username'],'playlist':personal_playlist_name(user['username']),'playlist_id':pid,'song_count':len(ids)}

@app.get('/v1/search')
async def catalog_search(artist:str,title:str,user=Depends(require_user)):
    exact,candidates=await search(user,artist,title)
    _,myids=await playlist_ids(user)
    def clean(s): return {'id':s.get('id'),'artist':s.get('artist'),'title':s.get('title'),'album':s.get('album'),'in_my_music':str(s.get('id')) in myids}
    return {'exact_matches':[clean(s) for s in exact],'other_candidates':[clean(s) for s in candidates if s not in exact][:15]}

@app.post('/v1/my-music/add')
async def add_to_playlist(item:SelectSong,user=Depends(require_user)):
    song,added=await append_to_my_music(user,item.navidrome_song_id)
    with connection() as db:
        db.execute('''INSERT OR IGNORE INTO cassette_explicit_saves (user_id,song_id)
                      VALUES (?,?)''',(user['id'],str(song['id'])))
    return {'status':'added' if added else 'already_added','song':{'id':song.get('id'),'title':song.get('title'),'artist':song.get('artist')}}

@app.post('/v1/proposals')
async def propose(batch:ProposalBatch,user=Depends(require_user)):
    ids=[]
    with connection() as d:
        for s in batch.songs:
            id_=str(uuid.uuid4())
            try:
                d.execute('INSERT INTO proposals(id,user_id,artist,title,album,recording_mbid,source,state,created) VALUES(?,?,?,?,?,?,?,?,?)',
                    (id_,user['id'],s.artist,s.title,s.album,s.recording_mbid,s.source,'proposed',int(time.time())))
                ids.append(id_)
            except sqlite3.IntegrityError:
                existing=d.execute('SELECT id FROM proposals WHERE user_id=? AND artist=? AND title=? AND album=?',(user['id'],s.artist,s.title,s.album)).fetchone()
                # Re-proposing a previously hidden song should make it visible again.
                d.execute('UPDATE proposals SET dismissed_at=NULL WHERE id=? AND user_id=?',
                          (existing['id'],user['id']))
                ids.append(existing['id'])
    return {'count':len(ids),'ids':ids,'status':'awaiting_user_approval'}

def request_status(state):
    states={
        'proposed':'Needs approval', 'new':'Searching', 'checking':'Verifying',
        'submitting':'Submitting', 'requested':'Queued / acquiring',
        'awaiting_approval':'Awaiting approval', 'importing':'Importing',
        'added':'Ready', 'ready':'Ready', 'needs_selection':'Needs review',
        'needs_metadata':'Needs review', 'needs_admin':'Needs review',
        'submission_uncertain':'Needs review', 'needs_relink':'Needs review',
        'needs_link':'Needs account link', 'rejected':'Rejected'
    }
    return states.get(state,'Needs review')


@app.get('/v1/proposals')
def list_proposals(user=Depends(require_user)):
    with connection() as d:
        rows=d.execute('SELECT id,artist,title,album,recording_mbid,source,state,nav_song_id,message,created FROM proposals WHERE user_id=? AND dismissed_at IS NULL ORDER BY created DESC LIMIT 500',(user['id'],)).fetchall()
        return {'proposals':[{**dict(r),
        'display_status':request_status(r['state'])} for r in rows]}

@app.delete('/v1/proposals/{proposal_id}')
def dismiss_proposal(proposal_id:str,user=Depends(require_user)):
    """Hide this profile's proposal from the list without cancelling a download.

    Keep the underlying row so reconciliation, retry deduplication and any
    already-submitted DroppedNeedle work continue unchanged.
    """
    with connection() as d:
        found=d.execute('UPDATE proposals SET dismissed_at=? WHERE id=? AND user_id=? AND dismissed_at IS NULL',
                        (int(time.time()),proposal_id,user['id']))
        if found.rowcount!=1:
            err('Proposal not found',404)
    return {'status':'removed_from_list',
            'message':'Removed from this list. Any DroppedNeedle download and automatic My Music reconciliation continue.'}

_clear_requests_lock=asyncio.Lock()

async def _cancel_native_request(user,mbid,kind):
    if not mbid: raise ValueError('No verified request identifier is available')
    token=linked_token(connection,cipher,user['id'])
    if not token: raise ValueError('Connect the request owner’s DroppedNeedle account to cancel')
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
            response=await client.delete(f'{DN}/api/v1/requests/active/{quote(mbid,safe="")}',
                params={'request_kind':kind},headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            result=response.json()
    except (httpx.HTTPError,ValueError,TypeError) as exc:
        raise ValueError('DroppedNeedle did not confirm cancellation') from exc
    if not isinstance(result,dict): raise ValueError('DroppedNeedle returned an unclear cancellation response')
    if result.get('success'): return
    message=str(result.get('message') or '')
    # A completed/failed/cancelled request has no live transfer to stop.
    if any(f"status '{state}'" in message.lower() for state in ('completed','failed','cancelled','rejected')):
        return
    raise ValueError(message or 'DroppedNeedle did not confirm cancellation')

def _reserve_shared_clear(proposal):
    """Reserve cancellation atomically, or detach when another listener needs it."""
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        shared=db.execute('''SELECT * FROM shared_track_acquisitions WHERE owner_id=?
            UNION ALL SELECT a.* FROM shared_track_followers f JOIN shared_track_acquisitions a
              ON a.recording_mbid=f.recording_mbid AND a.release_id=f.release_id
              WHERE f.request_id=? LIMIT 1''',(proposal['id'],proposal['id'])).fetchone()
        if not shared:return None,False
        shared=dict(shared)
        other=db.execute('''SELECT 1 FROM proposals p WHERE p.id!=? AND p.dismissed_at IS NULL
            AND p.state IN ('requested','submitting','submission_uncertain') AND
            (p.id=? OR p.id IN (SELECT request_id FROM shared_track_followers
             WHERE recording_mbid=? AND release_id=?)) LIMIT 1''',
            (proposal['id'],shared['owner_id'],shared['recording_mbid'],shared['release_id'])).fetchone()
        if other:
            return shared,True
        if shared['state'] in ('submitting','cancelling'):
            raise ValueError('A shared request is still being submitted or cancelled')
        db.execute("UPDATE shared_track_acquisitions SET state='cancelling',updated=? WHERE recording_mbid=? AND release_id=?",
                   (int(time.time()),shared['recording_mbid'],shared['release_id']))
        return shared,False

async def _clear_proposal(user,proposal):
    state=proposal['state'];shared=None
    if state=='submitting': raise ValueError('Submission is still in progress')
    if state in ('requested','submission_uncertain'):
        shared,detached=_reserve_shared_clear(proposal)
        if not shared and proposal['recording_mbid']:
            with connection() as db:
                other=db.execute('''SELECT 1 FROM proposals WHERE user_id!=? AND recording_mbid=?
                    AND dismissed_at IS NULL AND state IN ('requested','submitting','submission_uncertain') LIMIT 1''',
                    (user['id'],proposal['recording_mbid'])).fetchone()
            detached=bool(other)
        if not detached:
            try:
                owner=user
                if shared and shared['owner_id']!=proposal['id']:
                    with connection() as db:
                        row=db.execute('SELECT users.* FROM users JOIN proposals ON proposals.user_id=users.id WHERE proposals.id=?',
                                       (shared['owner_id'],)).fetchone()
                    if not row: raise ValueError('Shared request owner is unavailable')
                    owner=dict(row)
                await _cancel_native_request(owner,(shared or {}).get('recording_mbid') or proposal['recording_mbid'],'track')
            except ValueError:
                if shared:
                    with connection() as db:
                        db.execute('UPDATE shared_track_acquisitions SET state=?,updated=? WHERE recording_mbid=? AND release_id=?',
                                   (shared['state'],int(time.time()),shared['recording_mbid'],shared['release_id']))
                raise
            if shared:
                with connection() as db:
                    db.execute("UPDATE shared_track_acquisitions SET state='rejected',updated=? WHERE recording_mbid=? AND release_id=?",
                               (int(time.time()),shared['recording_mbid'],shared['release_id']))
    with connection() as db:
        changed=db.execute("UPDATE proposals SET dismissed_at=?,state=CASE WHEN state IN ('requested','submission_uncertain') THEN 'cancelled' ELSE state END WHERE id=? AND user_id=? AND dismissed_at IS NULL AND state=?",
                   (int(time.time()),proposal['id'],user['id'],state))
        if changed.rowcount!=1: raise ValueError('Request changed while clearing; try again')
        db.execute('DELETE FROM shared_track_followers WHERE request_id=?',(proposal['id'],))

async def _clear_album(user,album):
    state=album['state'];group=album['release_group_mbid']
    if state in ('submitting','checking'): raise ValueError('Album submission is still in progress')
    if state in ('requested','awaiting_approval','importing','submission_uncertain'):
        with connection() as db:
            other=db.execute('''SELECT 1 FROM cassette_album_requests WHERE user_id!=? AND release_group_mbid=?
                AND save_desired=1 AND dismissed_at IS NULL AND state IN ('requested','awaiting_approval','importing','submitting','submission_uncertain') LIMIT 1''',
                (user['id'],group)).fetchone()
        if not other: await _cancel_native_request(user,group,'album')
    with connection() as db:
        db.execute('''UPDATE cassette_album_requests SET dismissed_at=?,updated=?,
            save_desired=CASE WHEN state IN ('requested','awaiting_approval','importing','submission_uncertain') THEN 0 ELSE save_desired END,
            state=CASE WHEN state IN ('requested','awaiting_approval','importing','submission_uncertain') THEN 'cancelled' ELSE state END
            WHERE user_id=? AND release_group_mbid=? AND dismissed_at IS NULL''',
            (int(time.time()),int(time.time()),user['id'],group))

@app.post('/v1/requests/clear')
async def clear_requests(body:ClearRequestsIn,user=Depends(require_user)):
    import album_request_bridge_v6 as albums
    async with _clear_requests_lock, albums._album_lock:
        with connection() as db:
            proposals=[] if body.album_ids is not None else [dict(r) for r in db.execute(
                'SELECT * FROM proposals WHERE user_id=? AND dismissed_at IS NULL ORDER BY created',(user['id'],))]
            album_rows=[dict(r) for r in db.execute('SELECT * FROM cassette_album_requests WHERE user_id=? AND dismissed_at IS NULL ORDER BY created',(user['id'],))]
        if body.album_ids is not None: album_rows=[r for r in album_rows if r['release_group_mbid'] in body.album_ids]
        failures=[];cleared=0
        for item in proposals:
            try: await _clear_proposal(user,item);cleared+=1
            except ValueError as exc: failures.append({'title':item['title'],'reason':str(exc)})
        for item in album_rows:
            try: await _clear_album(user,item);cleared+=1
            except ValueError as exc: failures.append({'title':item['album'],'reason':str(exc)})
    return {'cleared':cleared,'remaining':failures}

def get_prop(uid,pid):
    with connection() as d:
        p=d.execute('SELECT * FROM proposals WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p: err('Proposal not found',404)
    return dict(p)

def update_prop(uid,pid,state,message='',nav_id=None):
    with connection() as d:
        d.execute('UPDATE proposals SET state=?,message=?,nav_song_id=? WHERE user_id=? AND id=?',(state,message,nav_id,uid,pid))

@app.post('/v1/proposals/{proposal_id}/reject')
def reject(proposal_id:str,user=Depends(require_user)):
    get_prop(user['id'],proposal_id)
    update_prop(user['id'],proposal_id,'rejected','Rejected by user')
    return {'status':'rejected'}

async def _submit_verified_cassette_track(user, proposal_id, p):
    """Match the original album edition before DroppedNeedle submission.

    An uncertain POST is never automatically retried. Only the explicit
    DroppedNeedle 'No download was started' error permits the next pair.
    """
    import edition_matching as editions

    token=linked_token(connection,cipher,user['id'])
    if not token:
        update_prop(user['id'],proposal_id,'needs_link',
                    'Link your own DroppedNeedle account before requesting music')
        return {'status':'needs_link','message':'Connect DroppedNeedle in Music Profiles Bridge'}

    mbid=str(p.get('recording_mbid') or '')
    if mbid and not editions.MBID.fullmatch(mbid):
        update_prop(user['id'],proposal_id,'needs_metadata',
                    'Invalid MusicBrainz recording ID; no download started')
        return {'status':'needs_metadata','message':'Invalid recording identifier'}
    request_album=p.get('album') or ''
    try:
        if request_album:
            request_album,candidates=await editions.resolve_cassette_album_track(
                user,p['artist'],p['title'],request_album)
            # The recording originally chosen by Cassette is preferred only
            # when it belongs to the exact verified edition.
            if mbid:
                candidates=([pair for pair in candidates if pair[0]==mbid]
                            + [pair for pair in candidates if pair[0]!=mbid])
        elif mbid:
            candidates=[(mbid,None)]
        else:
            recordings=await editions.dn_resolve_track(user,p['artist'],p['title'])
            candidates=[(recordings[0],None)] if len(recordings)==1 else []
    except HTTPException as exc:
        update_prop(user['id'],proposal_id,'needs_metadata',
                    'MusicBrainz edition lookup unavailable; try Approve/check later')
        return {'status':'needs_metadata','message':'MusicBrainz lookup failed; no download submitted'}
    except (httpx.HTTPError,ValueError,TypeError):
        update_prop(user['id'],proposal_id,'needs_metadata',
                    'MusicBrainz edition lookup failed; try Approve/check later')
        return {'status':'needs_metadata','message':'Unable to verify edition; no download submitted'}

    # BRIDGE_METADATA_RECOVERY_V1: original-album matching fails for legitimate
    # standalone singles and alternate recordings on compilations. Verify the
    # exact recording/release pair instead of immediately rejecting the request.
    if not candidates:
        try:
            from metadata_resolver_v1 import verified_recording_release_pairs
            candidates=await verified_recording_release_pairs(
                p['artist'],p['title'],request_album or p['title'],preferred_id=mbid or None)
        except (HTTPException,httpx.HTTPError,ValueError,TypeError) as exc:
            LOG.warning('Alternate recording metadata lookup unavailable: %s',type(exc).__name__)
            update_prop(user['id'],proposal_id,'needs_metadata',
                        'Alternate version metadata temporarily unavailable; no request submitted')
            return {'status':'needs_metadata','message':'Verified recording lookup unavailable'}
    if not candidates:
        update_prop(user['id'],proposal_id,'needs_metadata',
                    'No verified recording and release pair for this exact version; no download started')
        return {'status':'needs_metadata','message':'Cannot identify an exact album edition'}

    # One atomic transition prevents concurrent Approve requests from
    # launching duplicate DroppedNeedle submissions.
    with connection() as db:
        reserved=db.execute("""UPDATE proposals SET state='submitting',
            message='Submitting verified edition to DroppedNeedle'
            WHERE id=? AND user_id=? AND state IN
            ('proposed','needs_admin','needs_metadata','needs_link','needs_relink','needs_selection')""",
            (proposal_id,user['id']))
        if reserved.rowcount!=1:
            current=db.execute('SELECT state FROM proposals WHERE id=? AND user_id=?',
                               (proposal_id,user['id'])).fetchone()
            return {'status':current['state'] if current else 'not_found',
                    'message':'Request already being processed or requires review'}

    for entry in candidates[:editions.MB_RECORDING_CANDIDATES]:
        # Legacy studio-album pairs are two-tuples. The metadata fallback pins
        # the recording, its release, and that release's GROUP together.
        candidate,release_id=entry[:2]
        from shared_acquisition import claim,finish
        owner,shared=claim(connection,candidate,release_id,proposal_id,follower_kind='proposal')
        if not owner:
            if shared['state']=='cancelling':
                update_prop(user['id'],proposal_id,'needs_metadata',
                            'The shared acquisition is being cancelled. Please approve again shortly.')
                return {'status':'needs_metadata','message':'Shared acquisition is being cancelled'}
            state='submission_uncertain' if shared['state']=='uncertain' else 'requested'
            update_prop(user['id'],proposal_id,state,
                'Waiting for a family member’s verified acquisition of this recording and edition')
            with connection() as db:
                db.execute('UPDATE proposals SET recording_mbid=? WHERE id=?',(candidate,proposal_id))
            return {'status':state,'message':'Shared acquisition already in progress'}
        release_group_id=entry[2] if len(entry)>2 else None
        exact_album=entry[3] if len(entry)>3 else request_album
        body={'artist_name':p['artist'],'track_title':p['title']}
        if exact_album:body['album_title']=exact_album
        if release_id:body['release_id']=release_id
        if release_group_id:body['release_group_mbid']=release_group_id
        try:
            async with httpx.AsyncClient(timeout=25,trust_env=False,follow_redirects=False) as client:
                res=await client.post(f'{DN}/api/v1/tracks/{quote(candidate)}/request',
                    json=body,headers={'Authorization':f'Bearer {token}'})
        except httpx.HTTPError:
            finish(connection,candidate,release_id,proposal_id,'uncertain')
            update_prop(user['id'],proposal_id,'submission_uncertain',
                        'Submission outcome unknown. Check DroppedNeedle before retrying.')
            return {'status':'submission_uncertain','message':'Check DroppedNeedle before retrying'}

        if res.status_code==401:
            finish(connection,candidate,release_id,proposal_id,'rejected')
            update_prop(user['id'],proposal_id,'needs_relink',
                        'DroppedNeedle session expired. Reconnect your account.')
            return {'status':'needs_relink','message':'Reconnect DroppedNeedle'}
        if res.status_code==403:
            finish(connection,candidate,release_id,proposal_id,'rejected')
            update_prop(user['id'],proposal_id,'needs_admin',
                        'DroppedNeedle account is not permitted to submit this track')
            return {'status':'needs_admin','message':'DroppedNeedle permissions denied'}
        if res.status_code==400:
            try:payload=res.json()
            except ValueError:payload={}
            error=payload.get('error') if isinstance(payload,dict) else None
            details=error if isinstance(error,dict) else {}
            message=str(details.get('message') or payload.get('detail') or payload.get('message') or '') if isinstance(payload,dict) else ''
            code=details.get('code')
            LOG.warning('DroppedNeedle rejected exact track edition: status=400 code=%s',
                        str(code or 'unknown')[:48])
            if (code=='VALIDATION_ERROR' and
                'does not map to one unique track on the exact MusicBrainz edition' in message and
                'No download was started' in message):
                finish(connection,candidate,release_id,proposal_id,'rejected')
                # Only this explicit no-download response authorizes another POST.
                continue
            finish(connection,candidate,release_id,proposal_id,'uncertain')
            update_prop(user['id'],proposal_id,'submission_uncertain',
                        'DroppedNeedle rejected metadata with an unrecognized response. Inspect before retrying.')
            return {'status':'submission_uncertain','message':'Inspect DroppedNeedle before retrying'}
        if res.status_code>=400:
            finish(connection,candidate,release_id,proposal_id,'uncertain')
            update_prop(user['id'],proposal_id,'submission_uncertain',
                        f'DroppedNeedle HTTP {res.status_code}; submission outcome not confirmed')
            return {'status':'submission_uncertain','message':'Check DroppedNeedle before retrying'}
        try:result=res.json()
        except ValueError:
            finish(connection,candidate,release_id,proposal_id,'uncertain')
            update_prop(user['id'],proposal_id,'submission_uncertain',
                        'DroppedNeedle returned invalid JSON; inspect before retrying')
            return {'status':'submission_uncertain','message':'Unable to confirm request outcome'}
        if not isinstance(result,dict):
            finish(connection,candidate,release_id,proposal_id,'uncertain')
            update_prop(user['id'],proposal_id,'submission_uncertain',
                        'DroppedNeedle returned unexpected data; inspect before retrying')
            return {'status':'submission_uncertain','message':'Unable to confirm request outcome'}
        state=str(result.get('status') or 'requested')[:80]
        finish(connection,candidate,release_id,proposal_id,'accepted',result.get('task_id'))
        with connection() as db:
            db.execute('UPDATE proposals SET recording_mbid=? WHERE id=? AND user_id=?',
                       (candidate,proposal_id,user['id']))
        update_prop(user['id'],proposal_id,'requested',
                    f'DroppedNeedle accepted verified edition: {state}')
        return {'status':'requested','droppedneedle_status':state,
                'message':'Music will enter My Music after Navidrome indexes the recording'}

    update_prop(user['id'],proposal_id,'needs_metadata',
                'DroppedNeedle rejected all verified edition pairs and confirmed no download started')
    return {'status':'needs_metadata','message':'No verified edition accepted; no download started'}

@app.post('/v1/proposals/{proposal_id}/approve')
async def approve(proposal_id:str,user=Depends(require_user)):
    p=get_prop(user['id'],proposal_id)
    if p['state'] in ('added','rejected'):
        return {'status':p['state'],'message':'Already processed'}
    if p['state'] in ('submitting','submission_uncertain'):
        return {'status':p['state'],'message':'Check DroppedNeedle for an existing submission before retrying'}
    # A request may have completed since the previous 60-second reconciliation.
    # Let Approve/check detect arrival immediately instead of only returning
    # 'Already processed' for a request that has been downloaded.
    exact,_=await find_proposal_matches(user,p)
    if p['state']=='requested':
        if len(exact)==1:
            song,_=await append_to_my_music(user,str(exact[0]['id']))
            update_prop(user['id'],proposal_id,'added','Existing requested file added to My Music',str(song['id']))
            return {'status':'added','song_id':str(song['id']),'source':'shared_library'}
        return {'status':'requested','message':'Waiting for the recording to appear in Navidrome'}
    if len(exact)==1:
        song,_=await append_to_my_music(user,str(exact[0]['id']))
        update_prop(user['id'],proposal_id,'added','Existing shared file added to personal playlist',str(song['id']))
        return {'status':'added','song_id':str(song['id']),'source':'shared_library'}
    if len(exact)>1:
        update_prop(user['id'],proposal_id,'needs_selection','Multiple exact matches; select a specific song in /v1/search')
        return {'status':'needs_selection','candidates':[{'id':s['id'],'artist':s.get('artist'),'title':s.get('title'),'album':s.get('album')} for s in exact]}
    return await _submit_verified_cassette_track(user,proposal_id,p)

@app.post('/v1/proposals/{proposal_id}/choose')
async def choose(proposal_id:str,selection:SelectSong,user=Depends(require_user)):
    p=get_prop(user['id'],proposal_id)
    if p['state']=='rejected': err('Rejected proposal',409)
    checked=await sub(user,'getSong',{'id':selection.navidrome_song_id})
    candidate=checked.get('song',{})
    if normalize(candidate.get('artist'))!=normalize(p['artist']) or normalize(candidate.get('title'))!=normalize(p['title']): err('Chosen track does not match proposal',409)
    song,_=await append_to_my_music(user,selection.navidrome_song_id)
    update_prop(user['id'],proposal_id,'added','Matched exact selected track',str(song['id']))
    return {'status':'added','song_id':str(song['id'])}

@app.post('/v1/reconcile')
async def reconcile(user=Depends(require_user)):
    count=await reconcile_for_user(user)
    return {'added_count':count}

async def reconcile_for_user(user):
    from shared_acquisition import follower_state
    with connection() as d:
        waiting=[dict(r) for r in d.execute("SELECT id FROM proposals WHERE user_id=? AND state='requested' AND message LIKE 'Waiting for a family member%'",(user['id'],))]
    for item in waiting:
        claim_state=follower_state(connection,item['id'])
        if claim_state and claim_state['state']=='rejected':
            update_prop(user['id'],item['id'],'needs_metadata',
                        'Shared submission did not start; approve again to retry')
        elif claim_state and (claim_state['state']=='uncertain' or
              (claim_state['state']=='submitting' and claim_state['updated']<time.time()-1800)):
            update_prop(user['id'],item['id'],'submission_uncertain',
                        'Shared submission outcome uncertain; inspect DroppedNeedle before retrying')
    with connection() as d:
        rows=d.execute("SELECT * FROM proposals WHERE user_id=? AND state IN ('requested','needs_admin','needs_selection') LIMIT 100",(user['id'],)).fetchall()
    successes=0
    for p in rows:
        try:
            exact,_=await find_proposal_matches(user,dict(p))
            if len(exact)==1:
                song,_=await append_to_my_music(user,str(exact[0]['id']))
                update_prop(user['id'],p['id'],'added','Automatically added after arrival in shared library',str(song['id']))
                successes+=1
        except Exception as exc:
            LOG.warning('Reconciliation skipped proposal %s (%s)',p['id'],type(exc).__name__)
    # CASSETTE_ART_ALBUM_V6: also reconcile completed whole-album requests.
    try:
        successes+=await album_request_bridge_v6.reconcile_album_requests(user)
    except Exception as exc:
        LOG.warning('Album reconciliation skipped (%s)',type(exc).__name__)
    try:
        successes+=await metadata_recovery_v1.recheck_for_user(user)
    except Exception as exc:
        LOG.warning('Metadata recovery cycle skipped (%s)',type(exc).__name__)
    return successes

@app.on_event('startup')
async def startup_reconcile():
    async def loop():
        while True:
            await asyncio.sleep(max(30,min(600,int(os.getenv('PROFILE_RECONCILE_INTERVAL_SECONDS','60')))))
            with connection() as d:
                users=[dict(x) for x in d.execute('SELECT * FROM users')]
            for user in users:
                try: await reconcile_for_user(user)
                except Exception as exc: LOG.warning('Profile reconciliation failed for %s (%s)',user['username'],type(exc).__name__)
    app.state.worker=asyncio.create_task(loop())

@app.on_event('shutdown')
async def stop_reconcile():
    app.state.worker.cancel()
    try: await app.state.worker
    except asyncio.CancelledError: pass

@app.post('/v1/me/rotate-token')
async def rotate(user=Depends(require_user)):
    token=secrets.token_urlsafe(32)
    with connection() as d:
        d.execute('UPDATE users SET token_digest=? WHERE id=?',(digest(token),user['id']))
        d.execute('DELETE FROM profile_sessions WHERE user_id=?',(user['id'],))
    return {'profile_token':token,'message':'Old token invalidated. Save this token privately; it is shown once.'}

@app.post('/v1/me/reconnect')
async def reconnect_nav(data:ReconnectIn,user=Depends(require_user)):
    if data.navidrome_username.casefold()!=user['username'].casefold(): err('Username must match existing profile',409)
    candidate={'username':user['username'],'secret':cipher.encrypt(data.navidrome_password.encode())}
    await sub(candidate,'ping')
    await ensure_playlist(candidate)
    with connection() as d:
        d.execute('UPDATE users SET secret=? WHERE id=?',(candidate['secret'],user['id']))
    return {'status':'reconnected'}

@app.get('/',response_class=HTMLResponse,include_in_schema=False)
def home():
    path=os.path.join(os.path.dirname(__file__),'index.html')
    with open(path,encoding='utf-8') as f:return f.read()


# Register per-user album-request routes after Bridge initialization.
import album_request_bridge_v6

# Peer scheduler administration uses the shared durable ledger. It has its own
# admin-key gate and never submits music outside DroppedNeedle's native path.
from peer_scheduler_api import install as install_peer_scheduler
def scheduler_linked_token(authorization):
    user=require_user(authorization)
    return linked_token(connection,cipher,user['id'])
install_peer_scheduler(app, require_admin, scheduler_linked_token)

# BRIDGE_METADATA_RECOVERY_V1 recheck worker
import metadata_recovery_v1
