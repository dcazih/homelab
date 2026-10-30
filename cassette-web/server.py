"""Private same-origin gateway for the Cassette web port.

The browser only receives an opaque HttpOnly session cookie. Navidrome credentials
and Bridge tokens are encrypted at rest in the gateway's persistent key volume.
"""
import hashlib
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BRIDGE = os.getenv('BRIDGE_URL', 'http://music-profiles:8765').rstrip('/')
CASSETTE = os.getenv('CASSETTE_URL', 'http://cassette-proxy:8787').rstrip('/')
KEY_PATH = Path(os.getenv('SESSION_KEY_FILE', '/data/session.key'))
DIST = Path(__file__).parent / 'dist'
COOKIE = 'cassette_web_session'
ALLOWED = {
    'ping', 'getAlbumList2', 'getArtists', 'getArtist', 'getAlbum',
    'getPlaylists', 'getPlaylist', 'getStarred2', 'search3', 'getSong',
    'getRandomSongs', 'getSimilarSongs2', 'getLyricsBySongId',
    'getTopSongs', 'getMusicFolders', 'getNowPlaying', 'getUser',
    'star', 'unstar', 'scrobble', 'createPlaylist', 'updatePlaylist',
    'deletePlaylist', 'getInternetRadioStations', 'getPlayQueue',
    'savePlayQueue',
}
MEDIA = {'stream', 'getCoverArt', 'download'}
BRIDGE_PATHS = {'me', 'proposals', 'requests/live-status', 'requests/manual',
                'my-music/add', 'search', 'me/droppedneedle'}

app = FastAPI(title='Sona')


def cipher():
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


def session(request: Request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, 'Sign in required')
    try:
        import json
        return json.loads(cipher().decrypt(raw.encode(), ttl=60 * 60 * 24 * 90))
    except (InvalidToken, ValueError):
        raise HTTPException(401, 'Session expired')


def same_origin(request: Request):
    origin = request.headers.get('origin')
    if origin:
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if parsed.netloc != request.headers.get('host') or parsed.scheme not in ('http', 'https'):
            raise HTTPException(403, 'Cross-origin request denied')


def sub_auth(data):
    salt = secrets.token_hex(12)
    return {'u': data['username'], 't': hashlib.md5((data['password'] + salt).encode()).hexdigest(),
            's': salt, 'v': '1.16.1', 'c': 'cassette-web', 'f': 'json'}


class Login(BaseModel):
    username: str
    password: str


@app.post('/api/login')
async def login(body: Login, request: Request, response: Response):
    same_origin(request)
    async with httpx.AsyncClient(timeout=20) as client:
        result = await client.post(BRIDGE + '/v1/auth/login', json={
            'navidrome_username': body.username, 'navidrome_password': body.password})
    if result.status_code != 200:
        raise HTTPException(result.status_code, result.json().get('detail', 'Sign in failed'))
    import json
    payload = {'username': result.json()['username'], 'password': body.password,
               'bridge_token': result.json()['profile_token']}
    response.set_cookie(COOKIE, cipher().encrypt(json.dumps(payload).encode()).decode(),
                        httponly=True, secure=request.headers.get('host','').split(':')[0] not in ('localhost','127.0.0.1'), samesite='lax',
                        max_age=60 * 60 * 24 * 90, path='/')
    return {'username': payload['username']}


@app.get('/api/session')
async def current_session(request: Request):
    data = session(request)
    return {'username': data['username']}


@app.post('/api/logout')
async def logout(request: Request, response: Response):
    same_origin(request)
    data = session(request)
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(BRIDGE + '/v1/auth/logout', headers={'authorization': 'Bearer ' + data['bridge_token']})
    response.delete_cookie(COOKIE, path='/')
    return {'status': 'ok'}


@app.api_route('/api/sub/{endpoint}', methods=['GET', 'POST'])
async def subsonic(endpoint: str, request: Request):
    if endpoint not in ALLOWED:
        raise HTTPException(404)
    mutating = {'star','unstar','scrobble','createPlaylist','updatePlaylist','deletePlaylist','savePlayQueue'}
    if (endpoint in mutating and request.method != 'POST') or (endpoint not in mutating and request.method != 'GET'):
        raise HTTPException(405)
    if request.method == 'POST':
        same_origin(request)
    data = session(request)
    params = dict(request.query_params)
    params.update(sub_auth(data))
    async with httpx.AsyncClient(timeout=30) as client:
        result = await client.request(request.method, f'{CASSETTE}/rest/{endpoint}.view', params=params)
    try:
        payload = result.json().get('subsonic-response', {})
    except ValueError:
        raise HTTPException(502, 'Invalid music server response')
    if payload.get('status') != 'ok':
        raise HTTPException(502, payload.get('error', {}).get('message', 'Music server error'))
    return payload


@app.get('/api/media/{endpoint}')
async def media(endpoint: str, request: Request):
    if endpoint not in MEDIA:
        raise HTTPException(404)
    data = session(request)
    params = dict(request.query_params)
    params.update(sub_auth(data))
    # Keep the upstream connection alive until the browser finishes reading.
    client = httpx.AsyncClient(timeout=None)
    upstream = client.build_request('GET', f'{CASSETTE}/rest/{endpoint}.view', params=params,
                                    headers={k: request.headers[k] for k in ('range', 'if-range') if k in request.headers})
    result = await client.send(upstream, stream=True)
    if result.status_code >= 400:
        await result.aclose()
        await client.aclose()
        raise HTTPException(result.status_code, 'Media unavailable')

    async def body():
        try:
            async for chunk in result.aiter_bytes():
                yield chunk
        finally:
            await result.aclose()
            await client.aclose()

    headers = {k: v for k, v in result.headers.items() if k.lower() in
               ('accept-ranges', 'content-range', 'content-length', 'cache-control')}
    return StreamingResponse(body(), status_code=result.status_code,
                             media_type=result.headers.get('content-type', 'application/octet-stream'),
                             headers=headers)


@app.api_route('/api/bridge/{path:path}', methods=['GET', 'POST'])
async def bridge(path: str, request: Request):
    if path not in BRIDGE_PATHS and not (path.startswith('proposals/') and
        path.split('/')[-1] in ('approve', 'reject', 'recording-options', 'recording-choice')):
        raise HTTPException(404)
    if request.method == 'POST':
        same_origin(request)
    data = session(request)
    body = await request.body() if request.method == 'POST' else None
    async with httpx.AsyncClient(timeout=45) as client:
        result = await client.request(request.method, f'{BRIDGE}/v1/{path}',
            params=request.query_params, content=body,
            headers={'authorization': 'Bearer ' + data['bridge_token'],
                     'content-type': request.headers.get('content-type', 'application/json')})
    try:
        return Response(content=result.content, status_code=result.status_code,
                        media_type=result.headers.get('content-type', 'application/json'))
    except Exception:
        raise HTTPException(502, 'Profiles Bridge unavailable')


@app.get('/api/events/requests')
async def request_events(request: Request):
    data = session(request)
    client = httpx.AsyncClient(timeout=None)
    upstream = client.build_request('GET', BRIDGE + '/v1/requests/live-events',
                                    headers={'authorization': 'Bearer ' + data['bridge_token']})
    result = await client.send(upstream, stream=True)
    if result.status_code != 200:
        await result.aclose()
        await client.aclose()
        raise HTTPException(result.status_code, 'Request events unavailable')

    async def body():
        try:
            async for chunk in result.aiter_bytes():
                yield chunk
        finally:
            await result.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type='text/event-stream',
                             headers={'cache-control': 'no-cache', 'x-accel-buffering': 'no'})


@app.get('/health')
def health():
    return {'status': 'ok'}


if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/{path:path}', include_in_schema=False)
def frontend(path: str):
    requested = DIST / path
    if path and requested.is_file() and requested.resolve().is_relative_to(DIST.resolve()):
        return FileResponse(requested)
    return FileResponse(DIST / 'index.html')
