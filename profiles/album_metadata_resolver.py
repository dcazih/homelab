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
    # Real official multi-track releases can be unclassified in MusicBrainz.
    # The caller MUST separately verify an official edition with >= 2 tracks.
    # Known Singles/Broadcast/etc. still fail immediately.
    return (primary in _ALLOWED_TYPES
            or (primary == 'Other' and bool(secondary & _ALLOWED_OTHER))
            or primary is None)


def title_match(group, title):
    requested = fold(title)
    actual = str(group.get('title') or '')
    if not requested:
        return False
    if fold(actual) == requested:
        return True
    # Only permit a deliberately separated extension of the title, not a
    # generic prefix like "The Blueprint" vs "The Blueprint 2".
    if '//' in actual:
        return fold(actual.split('//', 1)[0]) == requested
    # The official canonical title sometimes includes an explicit descriptive
    # subtitle, e.g. "I Don't Like Shit, I Don't Go Outside: An Album by Earl
    # Sweatshirt". Verify the subtitle artist against the MusicBrainz credit.
    match = re.search(r':\\s*an album by\\s+(.+)


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



# Relation type IDs are stable MusicBrainz schema identifiers, not fuzzy labels.
_MEMBER_OF_BAND = '5be4c609-9afa-4ea0-910b-12ffb71e3821'
_IS_PERSON = 'dd9886f2-1dfe-4270-97db-283f6839a666'
_MAX_ARTIST_CANDIDATES = 4
_MAX_GROUP_RELATION_CHECKS = 9


def _identity_entities(artist_detail):
    """Same-person identities, proved only by MusicBrainz performance-name links."""
    own_id = str(artist_detail.get('id') or '')
    identities = {own_id} if MBID.fullmatch(own_id) else set()
    for rel in artist_detail.get('relations') or []:
        if rel.get('type-id') != _IS_PERSON:
            continue
        other = rel.get('artist') or {}
        other_id = str(other.get('id') or '')
        if MBID.fullmatch(other_id):
            identities.add(other_id)
    return identities


def _is_member_of(artist_detail, group_artist_ids):
    """A member-of-band link, rather than generic collaboration, proves membership."""
    for rel in artist_detail.get('relations') or []:
        if rel.get('type-id') != _MEMBER_OF_BAND or rel.get('direction') != 'forward':
            continue
        other = rel.get('artist') or {}
        if other.get('id') in group_artist_ids:
            return True
    return False


async def _verified_requested_artists(artist, mb_get):
    """Do not trust a high-ranked search hit without verifying its identity."""
    expected = fold(artist)
    queries = [
        f'artist:"{_escape_query(artist)}"',
        f'alias:"{_escape_query(artist)}"',
    ]
    seen, accepted = set(), []
    for query in queries:
        result = await mb_get('artist', {'query': query, 'limit': 20})
        inspected = 0
        for item in result.get('artists') or []:
            aid = str(item.get('id') or '')
            if not MBID.fullmatch(aid) or aid in seen:
                continue
            # The plain artist search may include hundreds of merely similar
            # names. Only the alias-specific query may inspect non-exact names.
            exact_name = fold(item.get('name')) == expected
            if not exact_name and not query.startswith('alias:'):
                continue
            inspected += 1
            if inspected > 5:
                break
            seen.add(aid)
            details = await mb_get('artist/' + aid, {'inc': 'aliases+artist-rels'})
            if not exact_name:
                actual = {fold(details.get('name'))}
                actual.update(fold(a.get('name')) for a in details.get('aliases') or []
                              if isinstance(a, dict))
                if expected not in actual:
                    continue
            if str(details.get('id') or '') != aid:
                continue
            accepted.append(details)
            if len(accepted) >= _MAX_ARTIST_CANDIDATES:
                return accepted
        if accepted:
            break
    return accepted


async def _group_relation_match(group, source_artists, mb_get, credited_cache, budget):
    """Prove group membership or shared performance identity via MB artist MBIDs."""
    target_ids = {str(p['artist'].get('id') or '')
                  for p in credit_parts(group.get('artist-credit'))}
    target_ids = {x for x in target_ids if MBID.fullmatch(x)}
    if not target_ids or not source_artists:
        return ''
    for source in source_artists:
        if _is_member_of(source, target_ids):
            return 'verified_group_membership'
    source_identity = set()
    for source in source_artists:
        source_identity.update(_identity_entities(source))
    for target_id in target_ids:
        if target_id in source_identity:
            return 'performance_name'
        if target_id not in credited_cache:
            if budget[0] <= 0:
                continue
            budget[0] -= 1
            credited_cache[target_id] = await mb_get('artist/' + target_id,
                                                     {'inc': 'aliases+artist-rels'})
        credited = credited_cache[target_id]
        # A second persona linked to the *same* underlying person is safe.
        if source_identity & _identity_entities(credited):
            return 'performance_name'
        # Sometimes only the group side documents a member relation. Reverse
        # direction is meaningful when checking a group artist's relations.
        if any(rel.get('type-id') == _MEMBER_OF_BAND
               and rel.get('direction') == 'backward'
               and str((rel.get('artist') or {}).get('id') or '') in {
                   str(src.get('id')) for src in source_artists}
               for rel in credited.get('relations') or []):
            return 'verified_group_membership'
    return ''

def _result(status, group=None, candidates=None, reason='', match_type=''):
    return {'status': status, 'group': group, 'candidates': candidates or [],
            'reason': reason, 'match_type': match_type}


async def resolve_album(artist, title, mb_get, release_group_mbid=None):
    """Resolve a verified original identity; otherwise return review candidates.

    This is metadata-only. Never creates DroppedNeedle requests, even if the
    source API returns a timeout or looks like a plausible fuzzy match.
    """
    if not fold(artist) or not fold(title):
        return _result('not_found', reason='Missing artist or album')

    alias_cache = {}
    alias_budget = [_MAX_ALIAS_LOOKUPS]
    seen, title_from_release = {}, set()

    async def accept(groups, allow_canonical_difference=False):
        for item in groups:
            if not isinstance(item, dict):
                continue
            gid = str(item.get('id') or '')
            if not MBID.fullmatch(gid):
                continue
            if title_match(item, title) or (allow_canonical_difference and gid in title_from_release):
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
        queries = [f'releasegroup:"{safe_title}" AND artist:"{safe_artist}"',
                   f'releasegroup:"{safe_title}"',
                   f'alias:"{safe_title}"',
                   f'release:"{safe_title}"']
        # Lucene phrase matching can miss punctuation spacing variants such
        # as "Legend: Soul" vs "Legend : Soul". Search the distinctive
        # second half, then verify the entire normalized title afterwards.
        if ':' in safe_title:
            tail = safe_title.split(':',1)[1].strip()
            if len(tail.split()) >= 2:
                queries.append(f'releasegroup:"{tail}"')
        # First search tightly; always run title-only if needed to discover
        # alternate group credits and distinct release-group collisions.
        for n, query in enumerate(queries):
            data = await mb_get('release-group', {'query': query, 'limit': _TITLE_QUERY_LIMIT})
            await accept(data.get('release-groups') or [])
            direct = [g for g in seen.values() if group_type_allowed(g)
                      and direct_credit_match(g, artist)]
            if len(direct) == 1 and n >= 1:
                break

        # As a last resort, a release's exact title may differ from the
        # canonical release-group title. Its real release-group MBID is proof.
        if not seen:
            data = await mb_get('release', {'query': f'release:"{safe_title}"', 'limit': 40})
            group_ids = []
            for release in data.get('releases') or []:
                if fold(release.get('title')) != fold(title):
                    continue
                group = release.get('release-group') or {}
                gid = str(group.get('id') or '') if isinstance(group, dict) else ''
                if MBID.fullmatch(gid) and gid not in group_ids:
                    group_ids.append(gid)
                if len(group_ids) >= 8:
                    break
            for gid in group_ids:
                title_from_release.add(gid)
                item = await mb_get('release-group/' + gid, {'inc': 'artist-credits'})
                await accept([item], allow_canonical_difference=True)

    seen = {gid:g for gid,g in seen.items() if group_type_allowed(g)}
    if not seen:
        return _result('not_found', reason='No usable release-group metadata found')
    eligible = []
    match_reason = {}
    for group in seen.values():
        gid=group['id']
        if direct_credit_match(group, artist):
            eligible.append(group)
            match_reason[gid]='artist_credit'
    if not eligible:
        # Search the requested artist identity ONCE. Matching artist MBIDs
        # automatically handles aliases and alternate release credits. Only
        # true MB artist relations may promote a member or another persona.
        sources = await _verified_requested_artists(artist, mb_get)
        source_ids = {str(x.get('id') or '') for x in sources}
        credited_cache = {}
        budget = [_MAX_GROUP_RELATION_CHECKS]
        for group in seen.values():
            target_ids={str(p['artist'].get('id') or '')
                        for p in credit_parts(group.get('artist-credit'))}
            gid=group['id']
            if source_ids & target_ids:
                eligible.append(group)
                match_reason[gid]='verified_artist_id'
                continue
            if sources:
                relation=await _group_relation_match(
                    group,sources,mb_get,credited_cache,budget)
                if relation:
                    eligible.append(group)
                    match_reason[gid]=relation
        # Only a tiny set of groups with no MusicBrainz artist identity can
        # warrant extra credited-artist alias verification; large result sets
        # otherwise trigger an unbounded fanout (e.g. artist "MIKE").
        if not eligible and not sources and len(seen) <= 3:
            for group in seen.values():
                if await _credit_has_verified_alias(
                        group,artist,mb_get,alias_cache,alias_budget):
                    eligible.append(group)
                    match_reason[group['id']]='artist_alias'

    if len(eligible) == 1:
        group=eligible[0]
        mtype=match_reason[group['id']]
        if group['id'] in title_from_release:
            mtype += '+release_title'
        elif fold(group.get('title')) != fold(title):
            mtype += '+canonical_title_extension'
        return _result('matched',group=group,match_type=mtype)
    if len(eligible) > 1:
        return _result('ambiguous',candidates=eligible,
                       reason='Multiple MusicBrainz release groups match verified credits/relationships')
    return _result('not_found',candidates=list(seen.values())[:8],
                   reason='Exact album title found, but artist identity is unverified')
, actual, flags=re.IGNORECASE)
    if match:
        prefix=actual[:match.start()]
        source_artist=fold(match.group(1))
        credited={fold(part.get('name') or part['artist'].get('name'))
                  for part in credit_parts(group.get('artist-credit'))}
        return fold(prefix)==requested and source_artist in credited
    return False


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



# Relation type IDs are stable MusicBrainz schema identifiers, not fuzzy labels.
_MEMBER_OF_BAND = '5be4c609-9afa-4ea0-910b-12ffb71e3821'
_IS_PERSON = 'dd9886f2-1dfe-4270-97db-283f6839a666'
_MAX_ARTIST_CANDIDATES = 4
_MAX_GROUP_RELATION_CHECKS = 9


def _identity_entities(artist_detail):
    """Same-person identities, proved only by MusicBrainz performance-name links."""
    own_id = str(artist_detail.get('id') or '')
    identities = {own_id} if MBID.fullmatch(own_id) else set()
    for rel in artist_detail.get('relations') or []:
        if rel.get('type-id') != _IS_PERSON:
            continue
        other = rel.get('artist') or {}
        other_id = str(other.get('id') or '')
        if MBID.fullmatch(other_id):
            identities.add(other_id)
    return identities


def _is_member_of(artist_detail, group_artist_ids):
    """A member-of-band link, rather than generic collaboration, proves membership."""
    for rel in artist_detail.get('relations') or []:
        if rel.get('type-id') != _MEMBER_OF_BAND or rel.get('direction') != 'forward':
            continue
        other = rel.get('artist') or {}
        if other.get('id') in group_artist_ids:
            return True
    return False


async def _verified_requested_artists(artist, mb_get):
    """Do not trust a high-ranked search hit without verifying its identity."""
    expected = fold(artist)
    queries = [
        f'artist:"{_escape_query(artist)}"',
        f'alias:"{_escape_query(artist)}"',
    ]
    seen, accepted = set(), []
    for query in queries:
        result = await mb_get('artist', {'query': query, 'limit': 20})
        inspected = 0
        for item in result.get('artists') or []:
            aid = str(item.get('id') or '')
            if not MBID.fullmatch(aid) or aid in seen:
                continue
            # The plain artist search may include hundreds of merely similar
            # names. Only the alias-specific query may inspect non-exact names.
            exact_name = fold(item.get('name')) == expected
            if not exact_name and not query.startswith('alias:'):
                continue
            inspected += 1
            if inspected > 5:
                break
            seen.add(aid)
            details = await mb_get('artist/' + aid, {'inc': 'aliases+artist-rels'})
            if not exact_name:
                actual = {fold(details.get('name'))}
                actual.update(fold(a.get('name')) for a in details.get('aliases') or []
                              if isinstance(a, dict))
                if expected not in actual:
                    continue
            if str(details.get('id') or '') != aid:
                continue
            accepted.append(details)
            if len(accepted) >= _MAX_ARTIST_CANDIDATES:
                return accepted
        if accepted:
            break
    return accepted


async def _group_relation_match(group, source_artists, mb_get, credited_cache, budget):
    """Prove group membership or shared performance identity via MB artist MBIDs."""
    target_ids = {str(p['artist'].get('id') or '')
                  for p in credit_parts(group.get('artist-credit'))}
    target_ids = {x for x in target_ids if MBID.fullmatch(x)}
    if not target_ids or not source_artists:
        return ''
    for source in source_artists:
        if _is_member_of(source, target_ids):
            return 'verified_group_membership'
    source_identity = set()
    for source in source_artists:
        source_identity.update(_identity_entities(source))
    for target_id in target_ids:
        if target_id in source_identity:
            return 'performance_name'
        if target_id not in credited_cache:
            if budget[0] <= 0:
                continue
            budget[0] -= 1
            credited_cache[target_id] = await mb_get('artist/' + target_id,
                                                     {'inc': 'aliases+artist-rels'})
        credited = credited_cache[target_id]
        # A second persona linked to the *same* underlying person is safe.
        if source_identity & _identity_entities(credited):
            return 'performance_name'
        # Sometimes only the group side documents a member relation. Reverse
        # direction is meaningful when checking a group artist's relations.
        if any(rel.get('type-id') == _MEMBER_OF_BAND
               and rel.get('direction') == 'backward'
               and str((rel.get('artist') or {}).get('id') or '') in {
                   str(src.get('id')) for src in source_artists}
               for rel in credited.get('relations') or []):
            return 'verified_group_membership'
    return ''

def _result(status, group=None, candidates=None, reason='', match_type=''):
    return {'status': status, 'group': group, 'candidates': candidates or [],
            'reason': reason, 'match_type': match_type}


async def resolve_album(artist, title, mb_get, release_group_mbid=None):
    """Resolve a verified original identity; otherwise return review candidates.

    This is metadata-only. Never creates DroppedNeedle requests, even if the
    source API returns a timeout or looks like a plausible fuzzy match.
    """
    if not fold(artist) or not fold(title):
        return _result('not_found', reason='Missing artist or album')

    alias_cache = {}
    alias_budget = [_MAX_ALIAS_LOOKUPS]
    seen, title_from_release = {}, set()

    async def accept(groups, allow_canonical_difference=False):
        for item in groups:
            if not isinstance(item, dict):
                continue
            gid = str(item.get('id') or '')
            if not MBID.fullmatch(gid):
                continue
            if title_match(item, title) or (allow_canonical_difference and gid in title_from_release):
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
        queries = [f'releasegroup:"{safe_title}" AND artist:"{safe_artist}"',
                   f'releasegroup:"{safe_title}"',
                   f'alias:"{safe_title}"',
                   f'release:"{safe_title}"']
        # First search tightly; always run title-only if needed to discover
        # alternate group credits and distinct release-group collisions.
        for n, query in enumerate(queries):
            data = await mb_get('release-group', {'query': query, 'limit': _TITLE_QUERY_LIMIT})
            await accept(data.get('release-groups') or [])
            direct = [g for g in seen.values() if group_type_allowed(g)
                      and direct_credit_match(g, artist)]
            if len(direct) == 1 and n >= 1:
                break

        # As a last resort, a release's exact title may differ from the
        # canonical release-group title. Its real release-group MBID is proof.
        if not seen:
            data = await mb_get('release', {'query': f'release:"{safe_title}"', 'limit': 40})
            group_ids = []
            for release in data.get('releases') or []:
                if fold(release.get('title')) != fold(title):
                    continue
                group = release.get('release-group') or {}
                gid = str(group.get('id') or '') if isinstance(group, dict) else ''
                if MBID.fullmatch(gid) and gid not in group_ids:
                    group_ids.append(gid)
                if len(group_ids) >= 8:
                    break
            for gid in group_ids:
                title_from_release.add(gid)
                item = await mb_get('release-group/' + gid, {'inc': 'artist-credits'})
                await accept([item], allow_canonical_difference=True)

    seen = {gid:g for gid,g in seen.items() if group_type_allowed(g)}
    if not seen:
        return _result('not_found', reason='No usable release-group metadata found')
    eligible = []
    match_reason = {}
    for group in seen.values():
        gid=group['id']
        if direct_credit_match(group, artist):
            eligible.append(group)
            match_reason[gid]='artist_credit'
    if not eligible:
        # Search the requested artist identity ONCE. Matching artist MBIDs
        # automatically handles aliases and alternate release credits. Only
        # true MB artist relations may promote a member or another persona.
        sources = await _verified_requested_artists(artist, mb_get)
        source_ids = {str(x.get('id') or '') for x in sources}
        credited_cache = {}
        budget = [_MAX_GROUP_RELATION_CHECKS]
        for group in seen.values():
            target_ids={str(p['artist'].get('id') or '')
                        for p in credit_parts(group.get('artist-credit'))}
            gid=group['id']
            if source_ids & target_ids:
                eligible.append(group)
                match_reason[gid]='verified_artist_id'
                continue
            if sources:
                relation=await _group_relation_match(
                    group,sources,mb_get,credited_cache,budget)
                if relation:
                    eligible.append(group)
                    match_reason[gid]=relation
        # Only a tiny set of groups with no MusicBrainz artist identity can
        # warrant extra credited-artist alias verification; large result sets
        # otherwise trigger an unbounded fanout (e.g. artist "MIKE").
        if not eligible and not sources and len(seen) <= 3:
            for group in seen.values():
                if await _credit_has_verified_alias(
                        group,artist,mb_get,alias_cache,alias_budget):
                    eligible.append(group)
                    match_reason[group['id']]='artist_alias'

    if len(eligible) == 1:
        group=eligible[0]
        mtype=match_reason[group['id']]
        if group['id'] in title_from_release:
            mtype += '+release_title'
        elif fold(group.get('title')) != fold(title):
            mtype += '+canonical_title_extension'
        return _result('matched',group=group,match_type=mtype)
    if len(eligible) > 1:
        return _result('ambiguous',candidates=eligible,
                       reason='Multiple MusicBrainz release groups match verified credits/relationships')
    return _result('not_found',candidates=list(seen.values())[:8],
                   reason='Exact album title found, but artist identity is unverified')
