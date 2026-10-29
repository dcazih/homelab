"""Proxy-owned cover-art lookups for MusicBrainz discovery and imported songs.

Only fixed UUID-derived Cover Art Archive URLs are fetched; user-provided URLs
and OpenSubsonic credentials are never forwarded to the artwork host.
"""
import logging
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi.responses import Response

from collection import connect

LOG = logging.getLogger('cassette_cover_art')
COVER_RE = re.compile(r'^cassette-art-(release|group)_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', re.I)
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
CACHE_PATH = Path(os.getenv('CASSETTE_COVER_CACHE', '/data/cassette-artwork'))
MAX_BYTES = 3 * 1024 * 1024
# The downloaded file may have a generic Navidrome cover ID with missing art.
# For positively matched new downloads, use the catalog cover from MusicBrainz only when the original is missing. Never replace a valid native cover by default.
OVERRIDE_MATCHED = os.getenv('CASSETTE_COVER_OVERRIDE_MATCHED','false').lower() not in ('false','no','0')


def cover_id_for(release_mbid='', release_group_mbid=''):
    if UUID_RE.fullmatch(str(release_mbid or '')):
        return 'cassette-art-release_'+str(release_mbid).lower()
    if UUID_RE.fullmatch(str(release_group_mbid or '')):
        return 'cassette-art-group_'+str(release_group_mbid).lower()
    return ''


def is_cover_id(value):
    return isinstance(value,str) and COVER_RE.fullmatch(value) is not None


def normalized(value):
    chars=unicodedata.normalize('NFKD',str(value or '').casefold())
    return ' '.join(re.findall('[a-z0-9]+',''.join(c for c in chars if not unicodedata.combining(c))))


def _indices():
    """Build request-local indexes; results are user-independent public metadata."""
    recordings={}
    by_title_artist={}
    by_album_artist={}
    by_title_album={}
    unique_albums={}
    try:
        with connect() as db:
            rows=db.execute('''SELECT recording_mbid,artist,title,album,cover_id FROM discovery
                               WHERE cover_id != '' ORDER BY created DESC LIMIT 10000''').fetchall()
    except sqlite3.OperationalError:
        return recordings,by_title_artist,by_album_artist,by_title_album,unique_albums
    for mbid,artist,title,album,cover in rows:
        if not is_cover_id(cover):continue
        t,a,al=normalized(title),normalized(artist),normalized(album)
        recordings.setdefault(str(mbid).lower(),cover)
        if t and a:by_title_artist.setdefault((t,a),cover)
        if t and al:by_title_album.setdefault((t,al),cover)
        if a and al:by_album_artist.setdefault((al,a),cover)
        if al:
            if al not in unique_albums:unique_albums[al]=cover
            elif unique_albums[al]!=cover:unique_albums[al]=None
    return recordings,by_title_artist,by_album_artist,by_title_album,unique_albums


def augment_payload(value):
    """Fill artwork for known songs/albums in search, playlist and album payloads.

    Does not touch media IDs or favorite status. Prefer recording MBID over text
    to prevent attaching the wrong cover to similarly named recordings.
    """
    recordings,by_title_artist,by_album_artist,by_title_album,unique_albums=_indices()
    if not recordings:return value
    def mutate(node):
        if isinstance(node,list):
            for entry in node:mutate(entry)
        elif isinstance(node,dict):
            ident=str(node.get('id',''))
            if not ident.startswith('cassette-request-') and not ident.startswith('cassette-art-'):
                title=normalized(node.get('title'))
                artist=normalized(node.get('artist'))
                album=normalized(node.get('album') or node.get('name'))
                recording=str(node.get('musicBrainzId') or '').lower()
                art=(recordings.get(recording) or
                     by_title_artist.get((title,artist)) or
                     by_title_album.get((title,album)))
                if not title and album:
                    art=by_album_artist.get((album,artist)) or unique_albums.get(album)
                if art and (OVERRIDE_MATCHED or not node.get('coverArt')):
                    node['coverArt']=art
            for child in node.values():
                if isinstance(child,(list,dict)):mutate(child)
    mutate(value)
    return value


def _allowed_cover_host(url):
    parts=urlsplit(url)
    hostname=(parts.hostname or '').lower()
    return parts.scheme=='https' and (hostname=='coverartarchive.org' or
                                     hostname=='archive.org' or hostname.endswith('.archive.org'))


def _fallback_cover_id(identifier):
    try:
        with connect() as db:
            row=db.execute("SELECT cover_fallback_id FROM discovery WHERE cover_id=? AND cover_fallback_id != '' LIMIT 1",(identifier,)).fetchone()
            return row[0] if row and is_cover_id(row[0]) else ''
    except sqlite3.OperationalError:
        return ''


async def _fetch_archive(identifier, requested):
    match=COVER_RE.fullmatch(identifier or '')
    if not match:return None
    kind,mbid=match.groups()
    route='release' if kind=='release' else 'release-group'
    url=f'https://coverartarchive.org/{route}/{mbid}/front-{requested}'
    try:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=7.0) as client:
            for _ in range(6):
                if not _allowed_cover_host(url):return None
                async with client.stream('GET',url,headers={
                        'Accept':'image/jpeg,image/*','User-Agent':'CassetteFamilyCover/1.0 (personal music player)'}) as resp:
                    if resp.status_code in (301,302,303,307,308):
                        location=resp.headers.get('location','')
                        if not location:return None
                        url=urljoin(url,location)
                        continue
                    if resp.status_code!=200 or 'image/' not in resp.headers.get('content-type',''):
                        return None
                    result=bytearray()
                    async for chunk in resp.aiter_bytes():
                        result.extend(chunk)
                        if len(result)>MAX_BYTES:return None
                    if result.startswith(b'\xff\xd8\xff'):
                        return bytes(result),'image/jpeg'
                    if result.startswith(b'\x89PNG\r\n'):
                        return bytes(result),'image/png'
                    return None
    except (httpx.HTTPError,OSError,ValueError) as exc:
        LOG.warning('Album artwork lookup failed: %s',type(exc).__name__)
    return None


async def serve_cover(identifier, size=250):
    """Fetch release art, then a known release-group cover if the release lacks one.

    All requested IDs are prevalidated public MusicBrainz UUIDs. We do not
    pass OpenSubsonic credentials to any external cover-art service.
    """
    if not is_cover_id(identifier):return Response(status_code=404)
    requested=500 if str(size).isdigit() and int(size)>300 else 250
    candidates=[identifier]
    backup=_fallback_cover_id(identifier)
    if backup and backup!=identifier:candidates.append(backup)
    for candidate in candidates:
        kind,mbid=COVER_RE.fullmatch(candidate).groups()
        path=CACHE_PATH/(f'{kind}_{mbid.lower()}_{requested}.jpg')
        try:
            if path.is_file() and path.stat().st_size:
                return Response(path.read_bytes(),media_type='image/jpeg',
                                headers={'Cache-Control':'private, max-age=86400'})
        except OSError:pass
        found=await _fetch_archive(candidate,requested)
        if found:
            payload,media=found
            if media=='image/jpeg':
                try:
                    CACHE_PATH.mkdir(parents=True,exist_ok=True)
                    temp=path.with_suffix('.tmp')
                    temp.write_bytes(payload)
                    os.replace(temp,path)
                except OSError:pass
            return Response(payload,media_type=media,
                            headers={'Cache-Control':'private, max-age=86400',
                                     'X-Cassette-Artwork-Source':candidate.split('_',1)[0]})
    LOG.info('No image found for authorized MusicBrainz cover ID')
    return Response(status_code=404,headers={'Cache-Control':'private, max-age=300'})
