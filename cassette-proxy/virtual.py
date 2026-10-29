"""Backward-compatible legacy Stage 3 virtual-ID rewriting.

Stage 3.1 issues real album/artist IDs in new personal lists. Existing Cassette
state may still hold old Stage 3 virtual IDs; rewrite those to their real IDs so
bookmarks and open screens load the full shared album or artist without error.
"""
import base64
import binascii
from urllib.parse import parse_qsl, urlencode

from personal import PersonalizationError

PREFIX = {'album': 'cassette-personal-album-v3_', 'artist': 'cassette-personal-artist-v3_'}
MAX_ID_BYTES = 512


def virtual_id(kind: str, real_id: str) -> str:
    if kind not in PREFIX or not real_id:
        raise ValueError('Invalid virtual identifier type or value')
    raw = real_id.encode('utf-8')
    if len(raw) > MAX_ID_BYTES:
        raise ValueError('Media identifier exceeds safe limit')
    return PREFIX[kind] + base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_id(value: str):
    """Return (kind, real ID), None for a real ID, or raise for malformed virtual IDs."""
    if not isinstance(value, str):
        return None
    for kind, prefix in PREFIX.items():
        if not value.startswith(prefix):
            continue
        token = value[len(prefix):]
        if not token or len(token) > 4 * MAX_ID_BYTES // 3 + 8 or any(
            c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
            for c in token
        ):
            raise PersonalizationError('Malformed virtual media ID')
        try:
            raw = base64.urlsafe_b64decode(token + '=' * ((-len(token)) % 4))
            real = raw.decode('utf-8')
        except (UnicodeError, binascii.Error, ValueError) as exc:
            raise PersonalizationError('Malformed virtual media ID') from exc
        if not real or '\x00' in real or len(raw) > MAX_ID_BYTES or virtual_id(kind, real) != value:
            raise PersonalizationError('Malformed virtual media ID')
        return kind, real
    return None


def as_virtual_album(obj):
    record = dict(obj)
    record['id'] = virtual_id('album', str(obj['id']))
    aid = record.get('artistId')
    if aid:
        record['artistId'] = virtual_id('artist', str(aid))
    return record


def as_virtual_artist(obj):
    record = dict(obj)
    record['id'] = virtual_id('artist', str(obj['id']))
    return record



# Only remap virtual IDs for calls Cassette makes to related metadata/actions.
# Never remap song ID values, stream, scrobble, or download endpoints.
ID_FIELDS = {
    'getAlbum': {'id': 'album'},
    'getArtist': {'id': 'artist'},
    'star': {'albumId': 'album', 'artistId': 'artist'},
    'unstar': {'albumId': 'album', 'artistId': 'artist'},
    'getSimilarSongs': {'id': 'album'},
    'getSimilarSongs2': {'id': 'artist'},
    'getArtistInfo': {'id': 'artist'},
    'getArtistInfo2': {'id': 'artist'},
    'getSimilarArtists': {'id': 'artist'},
    'getSimilarArtists2': {'id': 'artist'},
    'getTopSongs': {'id': 'artist'},
    'setRating': {'id': None},
}


def rewrite_virtual_request(request, kind, body):
    """Return replacement (query bytes, body bytes, merged pairs), or None.

    This is not used for transparent streaming; only listed metadata/mutation
    endpoints are eligible. Preserves repeated fields such as id/songIdToAdd.
    """
    query = list(request.query_params.multi_items())
    posted = []
    if body and request.method == 'POST':
        if request.headers.get('content-type', '').split(';')[0].strip().lower() == 'application/x-www-form-urlencoded':
            try:
                posted = parse_qsl(body.decode('utf-8'), keep_blank_values=True)
            except UnicodeDecodeError as exc:
                raise PersonalizationError('Invalid form encoding') from exc
    allowed = ID_FIELDS.get(kind, {})
    touched = False

    def mapped(pairs):
        nonlocal touched
        out = []
        for key, value in pairs:
            dec = decode_id(value) if key in ('id', 'albumId', 'artistId') else None
            if dec is not None:
                if key not in allowed or (allowed[key] is not None and allowed[key] != dec[0]):
                    raise PersonalizationError('Virtual ID is unsupported for this operation')
                value = dec[1]
                touched = True
            out.append((key, value))
        return out

    query = mapped(query)
    posted = mapped(posted)
    if not touched:
        return None
    # Never allow POST bodies to be silently discarded, or ambiguous users/format.
    if body and request.method == 'POST' and not posted:
        raise PersonalizationError('Cannot rewrite non-form request body')
    merged = query + posted
    for name in ('u', 't', 's', 'p', 'apiKey', 'f'):
        if sum(1 for k, _ in merged if k == name) > 1:
            raise PersonalizationError('Duplicate authentication or format parameter')
    return (urlencode(query).encode('ascii'), urlencode(posted).encode('ascii') if posted else body, merged)
