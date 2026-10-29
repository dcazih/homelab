"""Cassette Stage 3.1: personal lists, globally complete album/artist pages.

Typed Search, detail pages, and audio remain global. Song and album stars
add only; artist stars do not modify collection membership.
"""

import asyncio
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
import logging
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
from personal import personalized_response
from actions import handle_favorite
from collection import initialize as initialize_collection
from discovery import setup as setup_discovery, search_response, is_external_id, unavailable_response, external_detail, expanded_external_artist_detail, expanded_external_album_detail
from cover_art import is_cover_id, serve_cover, augment_payload
from virtual import rewrite_virtual_request
from personal import PersonalizationError, error_response

logger = logging.getLogger("cassette_proxy")
# HTTP client libraries sometimes log whole URLs, including Subsonic auth tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class NoCookies(DefaultCookiePolicy):
    # A shared HTTP connection pool must never replay one user's cookies to another.
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

ENDPOINT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.view)?$")
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
}
# Ignore supplied forwarding metadata: source of truth is configured upstream and the incoming socket.
UNTRUSTED_HEADERS = {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}


def clean_headers(headers):
    """Preserve end-to-end headers (Range, Authorization, Cookie, etc.)."""
    # RFC 9110 also specifies the tokens listed in a Connection header are hop-by-hop.
    dynamic = set()
    get_list = headers.get_list if hasattr(headers, "get_list") else headers.getlist
    pairs = headers.multi_items() if hasattr(headers, "multi_items") else headers.items()
    for val in get_list("connection"):
        dynamic.update(part.strip().lower() for part in val.split(","))
    bad = HOP_HEADERS | UNTRUSTED_HEADERS | dynamic
    return [(k, v) for k, v in pairs if k.lower() not in bad]


def validate_upstream(value):
    uri = urlsplit(value.rstrip("/"))
    if uri.scheme not in ("http", "https") or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
        raise ValueError("NAVIDROME_UPSTREAM must be an http(s) URL with no credentials, query, or fragment")
    return value.rstrip("/")


def redirect_location(value: str, upstream: str, request: Request) -> str | None:
    """Prevent a Navidrome upstream-origin redirect pointing at Docker-only hostnames.

    Relative Location headers pass untouched; external redirects are rejected.
    """
    loc = urlsplit(value)
    if not loc.scheme and not loc.netloc:
        return value
    origin = urlsplit(upstream)
    if (loc.scheme.lower(), loc.netloc.lower()) != (origin.scheme.lower(), origin.netloc.lower()):
        return None
    base_path = origin.path.rstrip("/")
    if base_path and not (loc.path == base_path or loc.path.startswith(base_path + "/")):
        return None
    path = loc.path[len(base_path):] if base_path else loc.path
    # Same protocol/host as the original request, including path and query.
    # Use an origin-relative Location so it also works over Tailscale HTTPS.
    return urlunsplit(("", "", path or "/", loc.query, loc.fragment))


def create_app(upstream: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    upstream = validate_upstream(upstream or os.getenv("NAVIDROME_UPSTREAM", "http://host.docker.internal:4533"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_collection()
        setup_discovery()
        from saved_favorites import initialize as initialize_saved_favorites
        initialize_saved_favorites()
        app.state.personal_cache = {}
        app.state.star_lock = asyncio.Lock()
        app.state.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=NoCookies()),
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="Cassette OpenSubsonic Proxy", version="0.4.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status":"ok","stage":"4","album_unstar_reconciles":True,"individual_song_unstar_reconciles":True,"external_search":bool(os.getenv("CASSETTE_BRIDGE_INTERNAL_KEY")),"external_star_request":bool(os.getenv("CASSETTE_BRIDGE_INTERNAL_KEY"))}

    @app.get("/ready")
    async def ready(request: Request):
        """Connectivity check only; Navidrome will return an API auth error inside HTTP 200."""
        try:
            response = await request.app.state.client.get(f"{upstream}/rest/ping.view", timeout=5.0)
            response.raise_for_status()
            return {"upstream_reachable": True, "authentication_checked": False}
        except (httpx.HTTPError, RuntimeError):
            return JSONResponse(status_code=503, content={"upstream_reachable": False})

    @app.api_route("/rest/{endpoint:path}", methods=["GET", "POST", "HEAD"])
    async def rest_proxy(endpoint: str, request: Request):
        if not ENDPOINT_PATTERN.fullmatch(endpoint):
            return JSONResponse(status_code=404, content={"error": "unknown_endpoint"})

        feature_on = os.getenv('CASSETTE_PERSONALIZATION_ENABLED', 'true').lower() not in ('false','0','no')
        kind = endpoint.removesuffix('.view')
        # Read POST bodies once when transformations may be required; never buffer audio.
        body = b''
        if feature_on and request.method == 'POST' and kind in (
            'search3', 'getArtists', 'getAlbumList2', 'getAlbum', 'getArtist',
            'star', 'unstar', 'getSong', 'getSimilarSongs', 'getSimilarSongs2',
            'getStarred2', 'getArtistInfo', 'getArtistInfo2', 'getSimilarArtists',
            'getSimilarArtists2', 'getTopSongs', 'setRating', 'scrobble'):
            body = await request.body()

        if feature_on and kind == 'search3':
            from personal import request_params
            try:
                parsed = request_params(request, body)
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)
            # Nonempty queries are user searches. Empty search3 is Cassette's
            # personal All Songs enumeration and must not hit external catalog.
            if (parsed.get('query') or '').strip():
                return await search_response(request, upstream, body)

        if feature_on and kind == 'getCoverArt':
            from personal import request_params, credential_params, nav_json
            try:
                parsed=request_params(request,body)
                artid=parsed.get('id','')
                if is_cover_id(artid):
                    # Authenticate with the signed-in Navidrome user before using
                    # the public Cover Art Archive, even for cached artwork.
                    await nav_json(request.app.state.client,upstream,'ping',credential_params(parsed))
                    return await serve_cover(artid,parsed.get('size','250'))
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)

        if feature_on and kind in ('getSong','getAlbum','getArtist','stream','download','getCoverArt'):
            from personal import request_params, credential_params, nav_json
            try:
                parsed = request_params(request,body)
                ext_id = parsed.get('id','')
                if (is_external_id(ext_id) or ext_id.startswith(('cassette-request-album_','cassette-request-artist_'))):
                    auth=credential_params(parsed)
                    await nav_json(request.app.state.client,upstream,'ping',auth)
                    if kind in ('getSong','getAlbum','getArtist'):
                        if kind == 'getAlbum':
                            return await expanded_external_album_detail(request,auth,ext_id)
                        if kind == 'getArtist':
                            return await expanded_external_artist_detail(request,auth,ext_id)
                        return external_detail(ext_id,kind,auth['u'])
                    # CASSETTE_SILENT_PLACEHOLDER_V7: serve valid audio for
                    # an unavailable track instead of erroring and skipping.
                    if kind == 'stream' and is_external_id(ext_id):
                        from placeholder_audio_v7 import stream_virtual_song
                        return await stream_virtual_song(request,upstream,auth,ext_id)
                    return unavailable_response()
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)

        if feature_on and kind == 'getStarred2':
            from personal import request_params, credential_params
            from saved_favorites import merged_get_starred2
            try:
                auth=credential_params(request_params(request,body))
                return await merged_get_starred2(request,upstream,auth)
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)
            except Exception:
                logger.exception('Could not reconcile saved favorites')
                return error_response('Favorites could not be loaded; retry',70,status_code=502)

        if feature_on and kind == 'getAlbum':
            from album_pages_stage5 import native_album_detail
            try:
                result = await native_album_detail(request, upstream, body)
                if result is not None:
                    return result
            except PersonalizationError as exc:
                return error_response(str(exc), exc.code)

        # Ignore virtual-placeholder scrobbles. A 9:11 stand-in must never
        # enter Navidrome play counts/history or contaminate real usage data.
        if feature_on and kind == 'scrobble':
            from personal import request_params, credential_params, nav_json
            try:
                parsed=request_params(request,body)
                if is_external_id(parsed.get('id','')):
                    await nav_json(request.app.state.client,upstream,'ping',credential_params(parsed))
                    return JSONResponse({'subsonic-response':{
                        'status':'ok','version':'1.16.1','type':'navidrome'}},
                        headers={'Cache-Control':'private, no-store'})
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)

        if feature_on and kind == 'getArtist':
            from personal import request_params, credential_params
            from artist_enrichment_v5 import merged_native_artist_page
            try:
                parsed=request_params(request,body)
                local_artist_id=parsed.get('id','')
                if local_artist_id and not local_artist_id.startswith('cassette-request-'):
                    return await merged_native_artist_page(
                        __import__('discovery'),request,upstream,credential_params(parsed),local_artist_id)
            except PersonalizationError as exc:
                return error_response(str(exc),exc.code)

        if feature_on and kind in ('search3', 'getArtists', 'getAlbumList2'):
            personalized = await personalized_response(request, upstream, endpoint, body)
            if personalized is not None:
                return personalized

        rewritten = None
        if feature_on:
            try:
                rewritten = rewrite_virtual_request(request, kind, body)
            except PersonalizationError as exc:
                return error_response(str(exc), exc.code)

        # Song and album stars add profile selections. Album unstar removes
        # album-added My Music tracks, preserving independently saved songs.
        if feature_on and kind in ('star','unstar'):
            result = await handle_favorite(request, upstream, endpoint, body, clean_headers, rewritten=rewritten)
            if result is not None:
                return result

        # Navidrome profile updates must be visible on the next personal-library read.
        if endpoint.removesuffix('.view') in ('updatePlaylist','createPlaylist','deletePlaylist'):
            request.app.state.personal_cache.clear()

        raw_query = (rewritten[0] if rewritten is not None else request.scope.get("query_string", b"")).decode("ascii")
        path = f"{upstream}/rest/{endpoint}"
        upstream_url = path + ("?" + raw_query if raw_query else "")
        request_headers = clean_headers(request.headers)
        if rewritten is not None:
            # Virtual IDs change URL/form byte lengths. Let httpx calculate the
            # new body length instead of forwarding Cassette's original length.
            request_headers = [(k,v) for k,v in request_headers
                               if k.lower() not in ('content-length','content-encoding')]

        try:
            content = (rewritten[1] if rewritten is not None else request.stream()) if request.method == "POST" else None
            outbound = request.app.state.client.build_request(
                request.method,
                upstream_url,
                headers=request_headers,
                content=content,
            )
            response = await request.app.state.client.send(outbound, stream=True)
        except (httpx.HTTPError, RuntimeError):
            # Never put URLs, raw query strings (they may contain passwords), or auth tokens in logs.
            logger.warning("Upstream request failed: endpoint=%s method=%s", endpoint, request.method)
            return JSONResponse(status_code=502, content={"error": "navidrome_unreachable"})

        # Cassette requests JSON metadata for album/search/playlist views. For
        # tracks imported after a DroppedNeedle request, Navidrome may have no
        # embedded cover art. Rewrite only JSON metadata, never audio or image
        # bodies (including HTTP Range responses).
        if (feature_on and kind in ('getSong','getAlbum','getPlaylist','getStarred2',
                                    'getAlbumList2','getRandomSongs') and request.method!='HEAD'
                and 'json' in response.headers.get('content-type','').lower()):
            try:
                raw=await response.aread()
                payload=json.loads(raw)
                if isinstance(payload,dict) and 'subsonic-response' in payload:
                    augment_payload(payload.get('subsonic-response'))
                    return JSONResponse(payload,status_code=response.status_code,
                        headers={'Cache-Control':'private, no-store'})
            except (ValueError,TypeError):
                logger.warning('Could not enhance cover metadata: endpoint=%s',kind)
            # Metadata is small; preserve exact upstream bytes if it was not JSON.
            return Response(content=response.content,status_code=response.status_code,
                            media_type=response.headers.get('content-type','application/json'),
                            headers={'Cache-Control':'private, no-store'})

        headers = clean_headers(response.headers)
        output_headers = []
        for key, value in headers:
            if key.lower() == "location":
                rewritten = redirect_location(value, upstream, request)
                if rewritten is None:
                    await response.aclose()
                    return JSONResponse(status_code=502, content={"error": "unsafe_upstream_redirect"})
                value = rewritten
            output_headers.append((key.encode("latin-1"), value.encode("latin-1")))

        async def chunks():
            try:
                # .aiter_raw keeps compression and byte ranges intact. Don't decode/reserialize music.
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        outgoing = StreamingResponse(chunks(), status_code=response.status_code)
        outgoing.raw_headers = output_headers
        return outgoing

    return app


app = create_app()
