"""Conservative multi-pass MusicBrainz album identity resolver.

Separates *finding an exact release-group identity* from *submitting a music
request*. Never submits downloads. Never guesses between distinct MBIDs.
"""
import logging
import re
import unicodedata

LOG = logging.getLogger('music_profiles.album_identity')
MBID = re.compile(r'^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')
_ALLOWED_TYPES = {'Album', 'EP'}
_ALLOWED_OTHER = {'Mixtape/Street', 'Compilation'}
_TITLE_QUERY_LIMIT = 60
_MAX_ALIAS_LOOKUPS = 6


def fold(value):
    """Conservative comparison: accent/punctuation and common volume variants."""
    s = unicodedata.normalize('NFKD', str(value or '').casefold())
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace('&', ' and ')
    s = re.sub(r'\bvol(?:ume)?\.?\s+', 'volume ', s)
    s = re.sub(r'\bpt\.?\s+', 'part ', s)
    s = re.sub(r'\b(volume|part)\s+(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b',
               lambda m: m.group(1) + ' ' + str({'i': 1, 'ii': 2,
               'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7,
               'viii': 8, 'ix': 9, 'x': 10}[m.group(2)]), s)
    return ' '.join(re.findall(r'[a-z0-9]+', s))


def credit_parts(credit):
    if not isinstance(credit, list):
        return []
    return [part for part in credit if isinstance(part, dict) and isinstance(part.get('artist'), dict)]


def credited_as(group):
    """Preserve MusicBrainz join phrases in the canonical release credit."""
    fragments = []
    for part in group.get('artist-credit') or []:
        if isinstance(part, str):
            fragments.append(part)
        elif isinstance(part, dict):
            artist = part.get('artist') or {}
            fragments.append(str(part.get('name') or artist.get('name') or ''))
            fragments.append(str(part.get('joinphrase') or ''))
    return ''.join(fragments).strip()


def direct_credit_match(group, artist):
    expected = fold(artist)
    return bool(expected) and any(expected in (fold(p.get('name')),
              fold(p['artist'].get('name'))) for p in credit_parts(group.get('artist-credit')))


def group_type_allowed(group):
    primary = group.get('primary-type')
    secondary = set(group.get('secondary-types') or [])
    return primary in _ALLOWED_TYPES or (primary == 'Other' and bool(secondary & _ALLOWED_OTHER))


def title_match(group, title):
    return bool(fold(title)) and fold(group.get('title')) == fold(title)


def _escape_query(value):
    # Quoted Lucene terms: avoid interpreting user-entered punctuation as operators.
    return re.sub(r'["\\]', ' ', str(value or '')).strip()


async def _credit_has_verified_alias(group, artist, mb_get, alias_cache, budget):
    """Only trust aliases attached to the *same MusicBrainz artist MBID*."""
    wanted = fold(artist)
    for part in credit_parts(group.get('artist-credit')):
        entity = part['artist']
        aid = str(entity.get('id') or '')
        if not MBID.fullmatch(aid):
            continue
        if aid not in alias_cache:
            if budget[0] <= 0:
                continue
            budget[0] -= 1
            details = await mb_get('artist/' + aid, {'inc': 'aliases'})
            aliases = details.get('aliases') or []
            aliases += details.get('alias-list') or []
            alias_cache[aid] = {fold(details.get('name'))} | {
                fold(a.get('name')) for a in aliases if isinstance(a, dict)}
        if wanted in alias_cache[aid]:
            return True
    return False


def _result(status, group=None, candidates=None, reason=''):
    return {'status': status, 'group': group, 'candidates': candidates or [], 'reason': reason}


async def resolve_album(artist, title, mb_get, release_group_mbid=None):
    """Resolve confidently or return actionable candidates without submitting.

    A supplied MBID pins the identity but is still checked against requested
    title and verified artist name/aliases. Multiple eligible groups always
    require selection, never a best-score guess.
    """
    if not fold(artist) or not fold(title):
        return _result('not_found', reason='Missing artist or album')

    alias_cache = {}
    alias_budget = [_MAX_ALIAS_LOOKUPS]
    seen = {}

    async def accept(groups):
        for item in groups:
            if not isinstance(item, dict):
                continue
            gid = str(item.get('id') or '')
            if MBID.fullmatch(gid) and gid not in seen and title_match(item, title):
                seen[gid] = item

    if release_group_mbid:
        if not MBID.fullmatch(str(release_group_mbid)):
            return _result('not_found', reason='Invalid release-group MBID')
        item = await mb_get('release-group/' + release_group_mbid, {'inc': 'artist-credits'})
        await accept([item])
        if not seen:
            return _result('not_found', reason='MBID points to a different album title')
    else:
        safe_title, safe_artist = _escape_query(title), _escape_query(artist)
        # First the narrow query, then title-only (catches artist punctuation,
        # alias indexing, and collaborative credits), then release title aliases.
        queries = [f'releasegroup:"{safe_title}" AND artist:"{safe_artist}"',
                   f'releasegroup:"{safe_title}"', f'alias:"{safe_title}"']
        for query in queries:
            data = await mb_get('release-group', {'query': query, 'limit': _TITLE_QUERY_LIMIT})
            await accept(data.get('release-groups') or [])
            direct = [g for g in seen.values() if group_type_allowed(g)
                      and direct_credit_match(g, artist)]
            # Search both strict and title-only to detect distinct exact matches.
            if len(direct) == 1 and query != queries[0]:
                break
        if not seen:
            data = await mb_get('release', {'query': f'release:"{safe_title}"', 'limit': 40})
            ids = []
            for release in data.get('releases') or []:
                if fold(release.get('title')) != fold(title):
                    continue
                group = release.get('release-group') or {}
                gid = str(group.get('id') or '') if isinstance(group, dict) else ''
                if MBID.fullmatch(gid) and gid not in ids:
                    ids.append(gid)
                if len(ids) >= 8:
                    break
            for gid in ids:
                item = await mb_get('release-group/' + gid, {'inc': 'artist-credits'})
                # Release titles can be aliases of the canonical group title.
                if MBID.fullmatch(str(item.get('id') or '')):
                    seen[gid] = item

    eligible = []
    for group in seen.values():
        if not group_type_allowed(group):
            continue
        if direct_credit_match(group, artist):
            eligible.append(group)
        elif await _credit_has_verified_alias(group, artist, mb_get, alias_cache, alias_budget):
            eligible.append(group)

    if len(eligible) == 1:
        return _result('matched', group=eligible[0])
    if len(eligible) > 1:
        return _result('ambiguous', candidates=eligible,
                       reason='Multiple distinct release groups match the artist and album')
    # Offer exact-title candidates for explicit selection; don't silently
    # attribute a collaboration or a persona's catalog to another artist.
    return _result('not_found', candidates=list(seen.values())[:8],
                   reason=('Matching album title found, but its artist credit/type differs'
                           if seen else 'No matching album metadata found'))
