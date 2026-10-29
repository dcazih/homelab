"""Cassette unavailable-track playback: one shared, authenticated 9:11 MP3.

The virtual IDs are NOT Navidrome songs. They never count as imported tracks,
never appear in My Music, and no placeholder bytes are written to the library.
"""

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi.responses import FileResponse, StreamingResponse

from collection import connect
from personal import error_response, nav_json, to_list

LOG = logging.getLogger('cassette_placeholder')
SILENCE = Path(__file__).with_name('unavailable-9m11s.mp3')
SILENCE_SECONDS = 551


def placeholder_file():
    """FileResponse supports ordinary HTTP Range playback in modern Starlette."""
    if not SILENCE.is_file():
        LOG.error('Configured silent placeholder is missing from proxy image')
        return error_response('Silent placeholder is not installed on the server')
    return FileResponse(
        SILENCE,
        media_type='audio/mpeg',
        headers={
            'Cache-Control': 'private, no-store',
            'X-Cassette-Placeholder': 'silent-9m11s',
            'X-Content-Type-Options': 'nosniff',
        },
    )


def lookup_virtual_track(track_id: str):
    with connect() as db:
        db.row_factory = __import__('sqlite3').Row
        row = db.execute('SELECT * FROM discovery WHERE id=?', (track_id,)).fetchone()
    return dict(row) if row is not None else None


async def _native_match(client, upstream: str, auth: dict, row: dict):
    """Allow a queued virtual ID to resolve to the REAL audio after import.

    Fail closed: only an exact MusicBrainz recording ID is sufficient. Matching
    by artist+title alone could play the non-acapella version instead.
    """
    recording = (row.get('recording_mbid') or '').strip().casefold()
    if not recording:
        return None
    try:
        root = await asyncio.wait_for(
            nav_json(client, upstream, 'search3', auth, {
                'query': row['title'], 'artistCount': '0',
                'albumCount': '0', 'songCount': '100',
            }), timeout=4.0,
        )
    except Exception as exc:
        LOG.info('No immediate native match for placeholder (%s)', type(exc).__name__)
        return None
    matches = [s for s in to_list((root.get('searchResult3') or {}).get('song'))
               if isinstance(s, dict)
               and s.get('id')
               and str(s.get('musicBrainzId') or '').casefold() == recording]
    if not matches:
        return None
    # Different copies of the SAME exact recording are fine. Stable choice.
    return sorted(matches, key=lambda s: str(s['id']))[0]['id']


async def stream_virtual_song(request, upstream: str, auth: dict, track_id: str):
    row = lookup_virtual_track(track_id)
    if row is None:
        return error_response('Unknown external recording; search again', 70)

    native_id = await _native_match(request.app.state.client, upstream, auth, row)
    if native_id:
        # No external redirect to the Docker-only Navidrome host, and no raw
        # credentials/URLs in logs. Use the existing authenticated client.
        params = {**auth, 'id': native_id}
        outgoing_headers = {}
        for name in ('range', 'if-range'):
            if request.headers.get(name):
                outgoing_headers[name] = request.headers[name]
        try:
            outbound = request.app.state.client.build_request(
                request.method,
                f'{upstream}/rest/stream.view',
                params=params,
                headers=outgoing_headers,
            )
            response = await request.app.state.client.send(outbound, stream=True)
            content_type = response.headers.get('content-type', '').lower()
            if response.status_code in (200, 206) and (
                content_type.startswith('audio/') or
                content_type.startswith('application/octet-stream')
            ):
                passthrough = {
                    name: response.headers[name]
                    for name in ('content-type', 'content-length', 'content-range',
                                 'accept-ranges', 'etag', 'last-modified')
                    if name in response.headers
                }
                passthrough['Cache-Control'] = 'private, no-store'

                async def data():
                    try:
                        if response.is_stream_consumed:
                            # In-memory transports return a pre-read response.
                            # Production socket responses stay streaming.
                            yield response.content
                        else:
                            async for chunk in response.aiter_raw():
                                yield chunk
                    finally:
                        await response.aclose()

                return StreamingResponse(data(), status_code=response.status_code,
                                         headers=passthrough)
            await response.aclose()
        except (httpx.HTTPError, RuntimeError) as exc:
            LOG.warning('Local recording not streamable, using silent placeholder (%s)',
                        type(exc).__name__)
        # Navidrome's scan may have indexed metadata before bytes were ready.
    return placeholder_file()
