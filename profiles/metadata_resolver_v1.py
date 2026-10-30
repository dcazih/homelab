# RECOVERY_V2_RELEASE_FIRST: strict release-first edition verification
"""Strict, release-first MusicBrainz identity recovery for family music requests.

The V1 helper trusted one recording-search query to enumerate every candidate.
This version can verify a song directly by reading release-group release tracklists,
including standalone singles and compilations containing variant recordings.
It never invents MusicBrainz IDs or downgrades the requested recording's version.

Entry point signature remains the same: Bridge app.py need not change.
"""
import logging
import re
import unicodedata

LOG = logging.getLogger('music_profiles.metadata_resolver')
_MAX_RELEASE_GROUPS = 5
_MAX_RELEASES = 12
_MAX_RECORDINGS = 5
_MAX_RECORDING_RELEASES = 8
_MAX_RESULTS = 3
_APPROVED_STATUSES = {'Official', 'Bootleg', 'Promotion'}
_STOPWORDS = {'the', 'a', 'an'}


def fold(value):
    v = unicodedata.normalize('NFKD', str(value or '').casefold())
    v = ''.join(c for c in v if not unicodedata.combining(c))
    v = re.sub(r'\ba[\s\-]+cappella\b', 'acapella', v)
    return ' '.join(re.findall(r'[a-z0-9]+', v))


def same_recording(requested, actual):
    return bool(fold(requested)) and fold(requested) == fold(actual)


def artist_matches(credit, artist):
    if not isinstance(credit, list):
        return False
    needle = fold(artist)
    for part in credit:
        if not isinstance(part, dict):
            continue
        performer = part.get('artist')
        if (isinstance(performer, dict) and fold(performer.get('name')) == needle) or fold(part.get('name')) == needle:
            return True
    return False


def _album_tokens(label, artist):
    a = set(fold(artist).split()) | _STOPWORDS
    return [t for t in fold(label).split() if t not in a]


def album_affinity(requested, observed, artist):
    if not requested or not observed:
        return False
    if fold(requested) == fold(observed):
        return True
    lhs, rhs = _album_tokens(requested, artist), _album_tokens(observed, artist)
    # Aliases with a prefixed artist/articles, but not loosely overlapping albums.
    return bool(lhs) and lhs == rhs


def _album_queries(album, artist):
    """Search literal, artist-stripped and token-conjunction variants.

    Search syntax must not depend on MusicBrainz matching "Foo Archive" to
    "The Foo Archive" as an exact phrase. All results are later verified.
    """
    original = ' '.join(str(album or '').split())
    stripped = ' '.join(_album_tokens(album, artist))
    choices = []
    for variant in [original, stripped]:
        if not variant:
            continue
        clean = variant.replace('"', '').strip()
        choices.append(f'releasegroup:"{clean}" AND artist:"{artist.replace(chr(34), "")}"')
    if len(stripped.split()) >= 2:
        conjunction = ' AND '.join(f'releasegroup:{w}' for w in stripped.split()[:5])
        choices.append(conjunction)  # Do not require artist on archive credits; verify release later.
    return list(dict.fromkeys(choices))


def _title_queries(title, artist):
    cleaned_title = str(title or '').replace('"', '').strip()
    cleaned_artist = str(artist or '').replace('"', '').strip()
    queries = [f'recording:"{cleaned_title}" AND artist:"{cleaned_artist}"']
    if 'acapella' in fold(title).split():
        # MusicBrainz records may use "a cappella" or "acapella".
        queries.append(f'recording:"{cleaned_title.replace("acapella", "a cappella")}" AND artist:"{cleaned_artist}"')
    return list(dict.fromkeys(queries))


def _mb_id(value):
    from edition_matching import MBID
    v = str(value or '')
    return v if MBID.fullmatch(v) else None


def _release_group_id(release, default=None):
    group = release.get('release-group') or release.get('release_group') or {}
    value = group.get('id') if isinstance(group, dict) else None
    return _mb_id(value) or _mb_id(default)


def _release_status(release):
    return release.get('status') or 'Official'


def _release_track_matches(release, artist, title, record_id=None):
    """Only recording IDs on the actual verified release are eligible."""
    matches = []
    for medium in release.get('media') or []:
        if not isinstance(medium, dict):
            continue
        for track in medium.get('tracks') or []:
            if not isinstance(track, dict):
                continue
            rec = track.get('recording') or {}
            if not isinstance(rec, dict):
                continue
            rid = _mb_id(rec.get('id'))
            if not rid or (record_id and rid != record_id):
                continue
            # Don't accept track titles that omit the requested variant.
            if not same_recording(title, rec.get('title')):
                continue
            credit = rec.get('artist-credit') or track.get('artist-credit')
            # A known exact artist at release-level is acceptable if the track
            # artist credit was not included in this MusicBrainz response.
            if credit and not artist_matches(credit, artist):
                continue
            if not credit and not artist_matches(release.get('artist-credit'), artist):
                continue
            if rid not in matches:
                matches.append(rid)
    return matches


async def _resolve_from_releases(artist, title, album, preferred_id):
    """Bounded verified release-group search independent of recording search."""
    from edition_matching import mb_get
    groups = []
    seen_groups = set()
    for query in _album_queries(album, artist):
        data = await mb_get('release-group', {'query':query, 'limit':35})
        for group in data.get('release-groups') or []:
            if not isinstance(group, dict):
                continue
            gid = _mb_id(group.get('id'))
            if not gid or gid in seen_groups:
                continue
            if not album_affinity(album, group.get('title'), artist):
                continue
            # The search query's artist is not proof. We check artist credits.
            # Some archives have different release-group credits; release track
            # credits must then explicitly confirm the requested performer.
            seen_groups.add(gid)
            groups.append(group)
        if len(groups) >= _MAX_RELEASE_GROUPS:
            break
    groups.sort(key=lambda g: (
        not artist_matches(g.get('artist-credit'), artist),
        fold(g.get('title')) != fold(album),
        g.get('first-release-date') or '9999',
        g.get('id', ''),
    ))
    verified = []
    attempted = 0
    for group in groups[:_MAX_RELEASE_GROUPS]:
        if attempted >= _MAX_RELEASES:
            break
        gid = str(group['id'])
        browse = await mb_get('release', {'release-group': gid, 'limit':75})
        entries = [r for r in browse.get('releases') or []
                   if isinstance(r, dict) and _mb_id(r.get('id'))
                   and album_affinity(album, r.get('title'), artist)
                   and _release_status(r) in _APPROVED_STATUSES]
        entries.sort(key=lambda r: (
            _release_status(r) != 'Official',
            fold(r.get('title')) != fold(album),
            str(r.get('date') or '9999'),
            str(r['id']),
        ))
        for entry in entries:
            if attempted >= _MAX_RELEASES:
                break
            attempted += 1
            release = await mb_get('release/' + str(entry['id']), {'inc':'recordings+artist-credits'})
            if not album_affinity(album, release.get('title'), artist):
                continue
            if _release_status(release) not in _APPROVED_STATUSES:
                continue
            # A genuine release-group ID comes from the browse query, but a
            # contradictory release ID must never be silently overridden.
            observed_gid = _release_group_id(release)
            if observed_gid and observed_gid != gid:
                continue
            canonical = str(release.get('title') or '').strip()
            if not canonical:
                continue
            found = _release_track_matches(release, artist, title)
            for rid in found:
                result = (rid, str(entry['id']), gid, canonical)
                if result not in verified:
                    verified.append(result)
            if preferred_id and any(x[0] == preferred_id for x in verified):
                break
            if len(verified) >= _MAX_RESULTS:
                break
        if preferred_id and any(x[0] == preferred_id for x in verified):
            break
        if len(verified) >= _MAX_RESULTS:
            break
    LOG.info('metadata_release_first artist=%s matched_groups=%s inspected_releases=%s verified_pairs=%s',
             artist, len(groups), attempted, len(verified))
    return verified


async def _resolve_from_recordings(artist, title, album, preferred_id):
    """Recording-first safety fallback when the release search wasn't exhaustive."""
    from edition_matching import mb_get
    record_ids = []
    if _mb_id(preferred_id):
        record_ids.append(preferred_id)
    for query in _title_queries(title, artist):
        data = await mb_get('recording', {'query':query, 'limit':50})
        for item in data.get('recordings') or []:
            if not isinstance(item, dict):
                continue
            rid = _mb_id(item.get('id'))
            if not rid or rid in record_ids:
                continue
            if not same_recording(title, item.get('title')):
                continue
            if not artist_matches(item.get('artist-credit'), artist):
                continue
            # Search scores can be absent. Exact title/artist and release
            # tracklist verification are stronger evidence than search rank.
            record_ids.append(rid)
        if len(record_ids) >= _MAX_RECORDINGS:
            break
    # Without an album hint, do not guess among different recordings.
    if not album:
        if preferred_id:
            record_ids = [preferred_id] if preferred_id in record_ids else []
        elif len(record_ids) != 1:
            return []
    confirmed = []
    inspected = 0
    for rid in record_ids[:_MAX_RECORDINGS]:
        record = await mb_get('recording/' + rid, {'inc':'releases+artist-credits'})
        if not same_recording(title, record.get('title')) or not artist_matches(record.get('artist-credit'), artist):
            continue
        releases = [entry for entry in record.get('releases') or []
                    if isinstance(entry, dict) and _mb_id(entry.get('id'))
                    and (not album or album_affinity(album, entry.get('title'), artist))
                    and _release_status(entry) in _APPROVED_STATUSES]
        releases.sort(key=lambda r: (
            _release_status(r) != 'Official',
            fold(r.get('title')) != fold(album),
            str(r.get('date') or '9999'),
            str(r['id']),
        ))
        for entry in releases:
            if inspected >= _MAX_RECORDING_RELEASES:
                break
            inspected += 1
            release = await mb_get('release/' + str(entry['id']), {'inc':'recordings+artist-credits+release-groups'})
            if album and not album_affinity(album, release.get('title'), artist):
                continue
            if not _release_track_matches(release, artist, title, record_id=rid):
                continue
            gid = _release_group_id(release)
            if not gid:
                # Resolve release group from browse group ID if response omits it.
                continue
            result = (rid, str(entry['id']), gid, str(release.get('title') or ''))
            if result not in confirmed:
                confirmed.append(result)
            if len(confirmed) >= _MAX_RESULTS:
                break
        if inspected >= _MAX_RECORDING_RELEASES or len(confirmed) >= _MAX_RESULTS:
            break
    LOG.info('metadata_recording_first artist=%s searched_recordings=%s inspected_releases=%s verified_pairs=%s',
             artist, len(record_ids), inspected, len(confirmed))
    return confirmed


async def verified_recording_release_pairs(artist, title, album, preferred_id=None):
    """Return strictly verified (recording ID, release ID, group ID, title) tuples.

    When the album is unknown, search recordings without inventing a release title;
    require an unambiguous recording or an explicit recording ID.
    """
    album = (album or '').strip()
    preferred = _mb_id(preferred_id)
    release_pairs = await _resolve_from_releases(artist, title, album, preferred) if album else []
    if not release_pairs:
        release_pairs = await _resolve_from_recordings(artist, title, album, preferred)
    if preferred:
        release_pairs.sort(key=lambda x: (x[0] != preferred, x[1]))
    return release_pairs[:_MAX_RESULTS]
