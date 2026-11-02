"""Family Music Muse connector: scoped OAuth2 + remote MCP gateway over existing Music Profiles Bridge.

No bridge/source/database changes. Never publishes the Bridge admin API.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

BASE = os.getenv('STAGE2_PUBLIC_URL', 'http://127.0.0.1:8788').rstrip('/')
BRIDGE = os.getenv('BRIDGE_URL', 'http://127.0.0.1:8765').rstrip('/')
DB = os.getenv('STAGE2_DB', '/data/stage2.sqlite3')
FERNET_KEY = os.getenv('STAGE2_ENCRYPTION_KEY', '')
DEV = os.getenv('STAGE2_DEV_MODE', '0') == '1'
SCOPE = 'music:read music:write'
# Meta Muse's fixed public OAuth callback. Muse does not use dynamic
# client registration; it sends a pre-issued client ID and PKCE verifier.
MUSE_CLIENT_ID = 'muse'
MUSE_REDIRECT_URI = 'https://agent.meta.ai/api/hatch/oauth/callback'
if not FERNET_KEY:
    raise RuntimeError('STAGE2_ENCRYPTION_KEY required; run setup_stage2.py')
if not (BASE.startswith('https://') or (DEV and BASE.startswith('http://127.0.0.1:'))):
    raise RuntimeError('STAGE2_PUBLIC_URL requires HTTPS in production')
cipher = Fernet(FERNET_KEY.encode())
app = FastAPI(title='Family Music Muse Connector', docs_url=None, redoc_url=None, openapi_url=None)

def now(): return int(time.time())
def sha(value): return hashlib.sha256(value.encode()).hexdigest()
def issue(): return secrets.token_urlsafe(32)

@contextmanager
def db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=10000')
    try:
        yield con
        con.commit()
    finally: con.close()

def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, name TEXT, redirect_uris TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_sessions (id_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, state TEXT, scope TEXT, resource TEXT, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_codes (digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, bridge_secret BLOB NOT NULL, username TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (access_hash TEXT PRIMARY KEY, refresh_hash TEXT UNIQUE NOT NULL, client_id TEXT NOT NULL, username TEXT NOT NULL, bridge_secret BLOB NOT NULL, scope TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        ''')
        # Every startup repairs the built-in Meta Muse public client, including
        # existing deployments with an already-created persistent SQLite DB.
        # No user sessions, Bridge credentials, or other clients are removed.
        c.execute('''
            INSERT INTO clients (id, name, redirect_uris, created)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, redirect_uris=excluded.redirect_uris
        ''', (MUSE_CLIENT_ID, 'Meta Muse', json.dumps([MUSE_REDIRECT_URI]), now()))
init()

@app.middleware('http')
async def security_headers(request: Request, next_call):
    response = await next_call(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

@app.get('/health')
async def health():
    # Health deliberately does not disclose user information or Bridge credentials.
    return {'status': 'ok', 'component':'muse-music-connector'}

@app.get('/.well-known/oauth-protected-resource')
@app.get('/.well-known/oauth-protected-resource/mcp')
async def resource_meta():
    return {'resource': BASE+'/mcp', 'authorization_servers':[BASE], 'scopes_supported':['music:read','music:write'], 'bearer_methods_supported':['header']}

@app.get('/.well-known/oauth-authorization-server')
async def auth_meta():
    return {'issuer':BASE, 'authorization_endpoint':BASE+'/authorize', 'token_endpoint':BASE+'/token',
            'registration_endpoint':BASE+'/register', 'revocation_endpoint':BASE+'/revoke',
            'response_types_supported':['code'], 'grant_types_supported':['authorization_code','refresh_token'],
            'token_endpoint_auth_methods_supported':['none'], 'code_challenge_methods_supported':['S256'],
            'scopes_supported':['music:read','music:write']}

def allowed_redirect(u):
    try:
        parsed=urlparse(u)
        if parsed.username or parsed.password or parsed.fragment or not parsed.netloc: return False
        if parsed.scheme=='https': return True
        return parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','[::1]')
    except ValueError: return False

class ClientRegistration(BaseModel):
    client_name: str = Field(default='Muse Music',max_length=120)
    redirect_uris: list[str] = Field(min_length=1,max_length=8)
    token_endpoint_auth_method: str = 'none'

_registrations={}

@app.post('/register', status_code=201)
async def register(reg: ClientRegistration, request:Request):
    ip=request.client.host if request.client else 'unknown'
    tick=time.monotonic()
    recent=[t for t in _registrations.get(ip,[]) if tick-t<3600]
    if len(recent)>=15:raise HTTPException(429,'Too many OAuth client registrations')
    _registrations[ip]=recent+[tick]
    if reg.token_endpoint_auth_method != 'none' or any(not allowed_redirect(r) for r in reg.redirect_uris):
        raise HTTPException(400,'Unsupported redirect URI or client authentication')
    with db() as c:
        if c.execute('SELECT count(*) FROM clients').fetchone()[0] >= 2000:
            raise HTTPException(429,'Client registration limit reached')
        client_id=issue()
        c.execute('INSERT INTO clients VALUES(?,?,?,?)',(client_id,reg.client_name,json.dumps(reg.redirect_uris),now()))
    return {'client_id':client_id,'client_name':reg.client_name,'redirect_uris':reg.redirect_uris,
            'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],'response_types':['code']}

def oauth_error(code, description=None, status=400):
    return JSONResponse({'error':code, **({'error_description':description} if description else {})},status_code=status)

def auth_page(tx_id, client_name, redirect_uri, scope):
    tx=html.escape(tx_id, quote=True)
    name=html.escape(client_name, quote=True)
    callback=html.escape(urlparse(redirect_uri).netloc, quote=True)
    grants=html.escape(scope, quote=True)
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Muse • Music Profiles</title>
<style>@font-face{{font-family:DM Sans;src:url('https://profiles.hippocampus-polaris.ts.net/assets/dm-sans.ttf') format('truetype');font-style:normal;font-weight:100 1000;font-display:swap}}@font-face{{font-family:Plus Jakarta Sans;src:url('https://profiles.hippocampus-polaris.ts.net/assets/plus-jakarta-sans-extrabold.ttf') format('truetype');font-weight:800;font-display:swap}}*{{box-sizing:border-box}}:root{{color-scheme:dark;--bg:#212121;--header:#171717;--surface:#2f2f2f;--input:#303030;--border:#424242;--text:#f4f4f4;--secondary:#b4b4b4;--muted:#8e8e8e;--primary:#f4f4f4;--primary-text:#171717;--focus:#777}}body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:16px 'DM Sans',system-ui,sans-serif}}.topbar{{height:61px;background:var(--header);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 max(20px,calc((100vw - 1180px)/2))}}.app-title{{font:800 18px 'Plus Jakarta Sans','DM Sans',sans-serif;user-select:none}}main{{width:min(430px,calc(100% - 40px));margin:7vh auto}}section{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:30px}}h1{{font-size:clamp(32px,4vw,44px);line-height:1.1;letter-spacing:-.035em;margin:0 0 10px;font-weight:700}}p{{font-size:15px;line-height:1.55;color:var(--secondary);margin:0 0 20px}}label{{display:block;font-size:14px;font-weight:550;color:var(--secondary);margin:14px 0 0}}input,button{{font:inherit}}input{{display:block;width:100%;margin:7px 0 0;padding:12px 14px;background:var(--input);color:var(--text);border:1px solid var(--border);border-radius:12px;caret-color:var(--text);transition:border-color 180ms ease,box-shadow 180ms ease}}input:hover{{border-color:#666}}input:focus{{outline:none;border-color:var(--focus);box-shadow:0 0 0 3px #7774}}button{{width:100%;border:0;border-radius:12px;margin-top:20px;padding:11px 16px;background:var(--primary);color:var(--primary-text);font-weight:600;font-size:14px;cursor:pointer;transition:box-shadow 180ms ease,transform 180ms ease}}button:hover{{box-shadow:inset 0 0 0 999px #00000016}}button:active{{transform:scale(.97)}}.context{{font-size:13px;color:var(--muted);margin:18px 0 0}}code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--secondary);overflow-wrap:anywhere}}@media(max-width:500px){{.topbar{{padding:0 16px}}main{{width:min(100% - 28px,430px);margin:28px auto}}section{{padding:24px 20px}}}}</style>
<body><header class="topbar"><strong class="app-title">Music Profiles</strong></header><main><section><h1>Authorize Muse</h1><p>Sign in with your Navidrome account to link <strong>{name}</strong>.</p><form method="post" action="/authorize/confirm"><input type="hidden" name="transaction" value="{tx}"><label>Navidrome username<input name="navidrome_username" autocomplete="username" required maxlength="80" autofocus></label><label>Navidrome password<input type="password" name="navidrome_password" autocomplete="current-password" required maxlength="256"></label><button type="submit">Authorize Music Tools</button></form><p class="context">Muse receives access only after this sign-in. Your Navidrome password is verified by Music Profiles and is never stored by Muse.<br><br>Callback: <code>{callback}</code> · Permissions: <code>{grants}</code></p></section></main></body></html>'''

@app.get('/authorize', response_class=HTMLResponse)
async def authorize(request:Request):
    q=request.query_params
    client_id=q.get('client_id','')
    redirect_uri=q.get('redirect_uri','')
    challenge=q.get('code_challenge','')
    state=q.get('state','')
    resource=q.get('resource',BASE+'/mcp')
    scope=q.get('scope',SCOPE)
    if (q.get('response_type')!='code' or q.get('code_challenge_method')!='S256' or
        not re.fullmatch('[A-Za-z0-9_-]{43,128}',challenge) or len(state)>2048 or
        resource not in (BASE+'/mcp', BASE) or any(s not in ('music:read','music:write') for s in scope.split()) or not scope.strip()):
        raise HTTPException(400,'Invalid OAuth authorization parameters')
    with db() as c:
        client=c.execute('SELECT * FROM clients WHERE id=?',(client_id,)).fetchone()
        if not client or redirect_uri not in json.loads(client['redirect_uris']):
            raise HTTPException(400,'Unknown client or redirect URI')
        tx=issue()
        c.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)',(sha(tx),client_id,redirect_uri,challenge,state,scope,resource,now()+600))
    return HTMLResponse(auth_page(tx, client['name'],redirect_uri,scope),headers={'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; font-src https://profiles.hippocampus-polaris.ts.net; frame-ancestors 'none'; base-uri 'none'"})

# In-memory login throttle. Production HTTPS required; Cloudflare rate limiting is also recommended.
_attempts={}

def login_throttle(ip):
    t=time.monotonic()
    values=[v for v in _attempts.get(ip,[]) if t-v<600]
    if len(values)>=8: raise HTTPException(429,'Too many authorization attempts; try again later')
    values.append(t)
    _attempts[ip]=values

async def bridge_get(path, profile, params=None):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r=await client.get(BRIDGE+path,params=params,headers={'Authorization':'Bearer '+profile})
            if r.status_code>=400: raise HTTPException(502,{'bridge_status':r.status_code,'detail':'Bridge rejected request'})
            return r.json()
    except (httpx.RequestError,ValueError) as e: raise HTTPException(502,'Bridge unreachable or invalid response') from e

async def bridge_post(path, profile, obj):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r=await client.post(BRIDGE+path,json=obj,headers={'Authorization':'Bearer '+profile})
            if r.status_code>=400: raise HTTPException(502,{'bridge_status':r.status_code,'detail':r.text[:300]})
            return r.json()
    except (httpx.RequestError,ValueError) as e: raise HTTPException(502,'Bridge unreachable or invalid response') from e

async def bridge_login(username, password):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r=await client.post(BRIDGE+'/v1/auth/login',json={
                'navidrome_username':username,
                'navidrome_password':password,
            })
            if r.status_code == 401:
                raise HTTPException(401,'Navidrome sign-in failed or this account has not joined Music Profiles')
            if r.status_code >= 400:
                raise HTTPException(502,'Music Profiles sign-in is temporarily unavailable')
            data=r.json()
            token=data.get('profile_token')
            if not isinstance(token,str) or not token:
                raise HTTPException(502,'Music Profiles did not create a session')
            return token, data.get('username','')
    except httpx.RequestError as e:
        raise HTTPException(502,'Music Profiles sign-in is unavailable') from e

@app.post('/authorize/confirm')
async def authorize_confirm(request:Request):
    login_throttle(request.client.host if request.client else 'unknown')
    form=await request.form()
    tx=str(form.get('transaction',''))
    username=str(form.get('navidrome_username','')).strip()
    password=str(form.get('navidrome_password',''))
    if len(tx)>256 or not 1 <= len(username) <= 80 or not 1 <= len(password) <= 256: raise HTTPException(400,'Invalid authorization form')
    with db() as c:
        sess=c.execute('SELECT * FROM auth_sessions WHERE id_hash=? AND expires>?',(sha(tx),now())).fetchone()
    if not sess: raise HTTPException(400,'Authorization expired. Start again from Muse.')
    try:
        profile_token, bridge_username=await bridge_login(username,password)
        me=await bridge_get('/v1/me',profile_token)
    except HTTPException:
        raise HTTPException(401,'Bridge profile token invalid or Bridge unavailable')
    username=me.get('username') or bridge_username
    if not username: raise HTTPException(502,'Bridge did not return a profile identity')
    code=issue()
    with db() as c:
        deleted=c.execute('DELETE FROM auth_sessions WHERE id_hash=? AND expires>?',(sha(tx),now()))
        if deleted.rowcount!=1: raise HTTPException(409,'Authorization already used')
        c.execute('INSERT INTO auth_codes VALUES(?,?,?,?,?,?,?,?,?)',
                  (sha(code),sess['client_id'],sess['redirect_uri'],sess['challenge'],cipher.encrypt(profile_token.encode()),username,sess['resource'],sess['scope'],now()+120))
    uri=sess['redirect_uri']
    sep='&' if '?' in uri else '?'
    return RedirectResponse(uri+sep+urlencode({'code':code,'state':sess['state']}), status_code=303, headers={'Referrer-Policy':'no-referrer'})

def valid_pkce(verifier, challenge):
    if not re.fullmatch('[A-Za-z0-9._~-]{43,128}',verifier or ''): return False
    actual=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return hmac.compare_digest(actual,challenge)

@app.post('/token')
async def token(request:Request):
    form=await request.form()
    grant=form.get('grant_type')
    cid=str(form.get('client_id',''))
    if grant=='authorization_code':
        code=str(form.get('code',''))
        redirect=str(form.get('redirect_uri',''))
        verifier=str(form.get('code_verifier',''))
        with db() as c:
            row=c.execute('SELECT * FROM auth_codes WHERE digest=? AND expires>?',(sha(code),now())).fetchone()
            if not row or row['client_id']!=cid or row['redirect_uri']!=redirect or not valid_pkce(verifier,row['challenge']):
                return oauth_error('invalid_grant')
            access,refresh=issue(),issue()
            changed=c.execute('DELETE FROM auth_codes WHERE digest=?',(sha(code),))
            if changed.rowcount!=1: return oauth_error('invalid_grant')
            c.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,0)',
                      (sha(access),sha(refresh),cid,row['username'],row['bridge_secret'],row['scope'],now()+3600))
        return {'access_token':access,'token_type':'Bearer','expires_in':3600,'refresh_token':refresh,'scope':row['scope']}
    if grant=='refresh_token':
        old=str(form.get('refresh_token',''))
        with db() as c:
            row=c.execute('SELECT * FROM grants WHERE refresh_hash=? AND client_id=? AND revoked=0',(sha(old),cid)).fetchone()
            if not row:return oauth_error('invalid_grant')
            access,refresh=issue(),issue()
            c.execute('UPDATE grants SET access_hash=?,refresh_hash=?,expires=? WHERE refresh_hash=?',
                      (sha(access),sha(refresh),now()+3600,sha(old)))
        return {'access_token':access,'token_type':'Bearer','expires_in':3600,'refresh_token':refresh,'scope':row['scope']}
    return oauth_error('unsupported_grant_type')

@app.post('/revoke')
async def revoke(request:Request):
    form=await request.form()
    token_=str(form.get('token',''))
    cid=str(form.get('client_id',''))
    with db() as c:
        c.execute('UPDATE grants SET revoked=1 WHERE client_id=? AND (refresh_hash=? OR access_hash=?)',(cid,sha(token_),sha(token_)))
    return Response(status_code=200)

def auth_grant(request):
    authorization=request.headers.get('authorization','')
    token_=authorization[7:].strip() if authorization.startswith('Bearer ') else ''
    with db() as c:
        row=c.execute('SELECT * FROM grants WHERE access_hash=? AND expires>? AND revoked=0',(sha(token_),now())).fetchone() if token_ else None
    if not row:
        raise HTTPException(401,'OAuth sign-in required',headers={'WWW-Authenticate':f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource"'})
    try: bridge_token=cipher.decrypt(row['bridge_secret']).decode()
    except InvalidToken: raise HTTPException(401,'Credential decrypt failed')
    return {'username':row['username'],'profile_token':bridge_token,'scope':row['scope']}

TOOLS=[
 {'name':'music_search','description':'Read-only. Search the authenticated family member’s Navidrome catalog by exact artist and track title. Never adds or requests music.',
  'inputSchema':{'type':'object','properties':{'artist':{'type':'string'},'title':{'type':'string'}},'required':['artist','title'],'additionalProperties':False}},
 {'name':'music_add_existing','description':'Write. Add an explicitly selected existing Navidrome song ID to the authenticated family member’s private My Music playlist. Does not duplicate audio files.',
  'inputSchema':{'type':'object','properties':{'song_id':{'type':'string'}},'required':['song_id'],'additionalProperties':False}},
 {'name':'music_request_song','description':'Write. Submit a track request to the authorized family profile via Bridge v2 /v1/requests. The Bridge decides whether it can reach DroppedNeedle or needs user action. Never claim downloaded unless confirmed.',
  'inputSchema':{'type':'object','properties':{'artist':{'type':'string'},'title':{'type':'string'},'album':{'type':'string'},'recording_mbid':{'type':'string'}},'required':['artist','title'],'additionalProperties':False}},
 {'name':'music_request_album','description':'Write. Submit an album request to the authorized family profile via Bridge v2 /v1/requests. The optional ID must be a release-GROUP MBID, not a release MBID. Never claim downloaded unless confirmed.',
  'inputSchema':{'type':'object','properties':{'artist':{'type':'string'},'album':{'type':'string'},'release_group_mbid':{'type':'string'}},'required':['artist','album'],'additionalProperties':False}},
 {'name':'music_list_requests','description':'Read-only. List this family member’s pending and completed Bridge music requests and proposals.',
  'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
 {'name':'music_get_request','description':'Read-only. Check a specific Bridge v2 request by its ID.',
  'inputSchema':{'type':'object','properties':{'request_id':{'type':'string'}},'required':['request_id'],'additionalProperties':False}}
]

def get_schema(spec,root):
    seen=set()
    for _ in range(6):
        if not isinstance(root,dict): return {}
        if '$ref' not in root: return root
        ref=root['$ref']
        if ref in seen or not ref.startswith('#/components/schemas/'):return {}
        seen.add(ref)
        root=spec.get('components',{}).get('schemas',{}).get(ref.split('/')[-1],{})
    return {}

async def album_request(profile, artist, album, release_group_mbid=''):
    # MUSE_DIRECT_BRIDGE_V2_REQUESTS_V1
    # Confirmed from live Bridge v2 OpenAPI: kind, artist, title are required.
    # 'title' is the album name for kind=album. No release ID is required.
    if len(artist) > 180 or len(album) > 240:
        raise HTTPException(422, 'Artist or album exceeds Bridge v2 field limits')
    payload = {'kind': 'album', 'artist': artist, 'title': album}
    if release_group_mbid:
        if len(release_group_mbid) > 36:
            raise HTTPException(422, 'Release-group MBID too long')
        payload['release_group_mbid'] = release_group_mbid
    response = await bridge_post('/v1/requests/manual', profile, payload)
    return {'bridge_response': response, 'note': 'Bridge response only. Do not claim DroppedNeedle acceptance or download until confirmed.'}


async def execute_tool(name,args,profile):
    if not isinstance(args,dict): raise HTTPException(400,'Tool arguments must be an object')
    tool=next((t for t in TOOLS if t['name']==name),None)
    if not tool: raise HTTPException(404,'Unknown tool')
    schema=tool['inputSchema']
    if set(args)-set(schema['properties']) or any(k not in args for k in schema.get('required',[])):
        raise HTTPException(422,'Invalid tool arguments')
    if any(not isinstance(v,str) or not v.strip() or len(v)>250 for v in args.values()):
        raise HTTPException(422,'Music identifiers must be nonempty strings under 251 characters')
    if name=='music_search': return await bridge_get('/v1/search',profile,{'artist':args['artist'],'title':args['title']})
    if name=='music_add_existing':return await bridge_post('/v1/my-music/add',profile,{'navidrome_song_id':args['song_id']})
    if name=='music_request_song':
        # MUSE_DIRECT_BRIDGE_V2_REQUESTS_V1
        # Old /v1/proposals path did not dispatch through per-user v2 acquisition.
        if len(args['artist']) > 180 or len(args['title']) > 240 or len(args.get('album','')) > 240:
            raise HTTPException(422,'Artist/title/album exceeds Bridge v2 field limits')
        if len(args.get('recording_mbid','')) > 36:
            raise HTTPException(422,'Recording MBID too long')
        payload={'kind':'song', 'artist':args['artist'], 'title':args['title']}
        if args.get('album'):
            payload['album']=args['album']
        if args.get('recording_mbid'):
            payload['recording_mbid']=args['recording_mbid']
        response=await bridge_post('/v1/requests/manual',profile,payload)
        return {'bridge_response':response,'note':'Bridge response only. Check request status before claiming DroppedNeedle acceptance or download.'}
    if name=='music_request_album':
        return await album_request(profile,args['artist'],args['album'],args.get('release_group_mbid',''))
    if name=='music_list_requests':
        output={}
        for kind,path in (('requests','/v1/proposals'),):
            try:output[kind]=await bridge_get(path,profile)
            except HTTPException as e:output[kind]={'error':e.detail}
        return output
    if name=='music_get_request':
        if not re.fullmatch('[A-Za-z0-9_-]{1,80}',args['request_id']):raise HTTPException(422,'Invalid request ID')
        # The Bridge exposes GET /v1/proposals, not GET /v1/proposals/{id}.
        # Only search the authenticated user's own proposals.
        proposals=await bridge_get('/v1/proposals',profile)
        found=next((item for item in proposals.get('proposals',[])
                    if str(item.get('id'))==args['request_id']),None)
        if found is None: raise HTTPException(404,'Request not found for this profile')
        return found

@app.post('/mcp')
async def mcp(request:Request):
    grant=auth_grant(request)
    origin=request.headers.get('origin')
    if origin and origin not in (BASE, 'https://agent.meta.ai'):
        raise HTTPException(403,'Untrusted MCP Origin')
    try:body=await request.json()
    except ValueError:raise HTTPException(400,'Expected JSON-RPC body')
    if not isinstance(body,dict) or body.get('jsonrpc')!='2.0':raise HTTPException(400,'JSON-RPC 2.0 required')
    method=body.get('method','')
    req_id=body.get('id')
    if req_id is None: return Response(status_code=202)
    if method=='initialize':
        result={'protocolVersion':body.get('params',{}).get('protocolVersion','2025-06-18'),'serverInfo':{'name':'family-music-bridge','version':'2.0.0'},'capabilities':{'tools':{'listChanged':False}}}
    elif method=='ping':result={}
    elif method=='tools/list':result={'tools':TOOLS}
    elif method=='tools/call':
        params=body.get('params') or {}
        try:
            tool_name=params.get('name')
            write=tool_name in ('music_add_existing','music_request_song','music_request_album')
            scope='music:write' if write else 'music:read'
            if scope not in grant['scope'].split(): raise HTTPException(403,'OAuth scope does not allow this tool')
            data=await execute_tool(tool_name,params.get('arguments') or {},grant['profile_token'])
            result={'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False)}],'isError':False}
        except HTTPException as e:
            result={'content':[{'type':'text','text':json.dumps({'error':e.detail,'status_code':e.status_code})}],'isError':True}
    else:return JSONResponse({'jsonrpc':'2.0','id':req_id,'error':{'code':-32601,'message':'Method not found'}},status_code=200)
    return JSONResponse({'jsonrpc':'2.0','id':req_id,'result':result},headers={'MCP-Protocol-Version':'2025-06-18'})

@app.get('/mcp')
async def mcp_get(request:Request):
    # Muse discovers OAuth via the 401 Bearer challenge on both GET and POST.
    auth_grant(request)
    return Response(status_code=405)
