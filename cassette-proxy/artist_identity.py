"""Identity-first MusicBrainz search for external artist queries.

Uses real artist MBIDs (including artist aliases) and release-group credits.
Never decides a match from a substring of an unrelated artist's name.
All queries are authenticated by the caller before this module runs.
"""
import asyncio
import unicodedata
from datetime import datetime

MAX_SEARCH_ALBUMS = 5
MAX_SEARCH_SONGS = 5


def normalized_identity(value):
    """Unicode-aware case, accent, separator normalization for names/aliases."""
    raw=unicodedata.normalize('NFKD', str(value or '').casefold())
    raw=''.join(ch for ch in raw if not unicodedata.combining(ch))
    return unicodedata.normalize('NFC', ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in raw).split()))


def artist_credits_contain(credits, mbid):
    return any(isinstance(c, dict) and
               isinstance(c.get('artist'), dict) and
               str(c['artist'].get('id', '')).lower() == mbid.lower()
               for c in (credits or []))


def clean_credit(credits, fallback):
    # The display name may historically be Kanye West although MusicBrainz's
    # canonical artist name is now Ye. Keep the credited release name.
    if not isinstance(credits, list):
        return fallback
    return ''.join((str(c.get('name') or c.get('artist', {}).get('name') or '') +
                    str(c.get('joinphrase') or '')) for c in credits if isinstance(c,dict)).strip() or fallback


async def resolve_exact_artist(d, client, query):
    q = normalized_identity(query)
    if len(q) < 2 and q.isascii():
        return None
    safe = str(query).replace('\\', '').replace('"', '').strip()[:120]
    # Both name and alias are searched: Kanye West is an alias of Ye.
    data = await d._artist_mb_get(client, 'artist/',
        {'query':f'artist:"{safe}" OR alias:"{safe}"', 'limit':25})
    if not isinstance(data, dict):
        return None
    ranked=[]
    for candidate in data.get('artists', []):
        if not isinstance(candidate, dict):
            continue
        mbid=str(candidate.get('id') or '').lower()
        if not d.MBID.fullmatch(mbid):
            continue
        try:score=int(candidate.get('score') or 0)
        except (TypeError,ValueError):continue
        if score < 75:
            continue
        direct = normalized_identity(candidate.get('name')) == q
        aliases = [a.get('name','') for a in (candidate.get('aliases') or []) if isinstance(a,dict)]
        alias_exact = any(normalized_identity(a)==q for a in aliases)
        # Search hits do not consistently include alias arrays. Verify the top
        # alias candidates by their actual MusicBrainz artist details.
        if not direct and not alias_exact:
            ranked.append((score, False, candidate))
        else:
            ranked.append((score, direct, candidate))
    ranked.sort(key=lambda x:(x[1],x[0]),reverse=True)
    verified=[]
    for score, direct, candidate in ranked[:6]:
        aliases=[a.get('name','') for a in (candidate.get('aliases') or []) if isinstance(a,dict)]
        if not direct and not any(normalized_identity(a)==q for a in aliases):
            details=await d._artist_mb_get(client, 'artist/'+candidate['id'], {'inc':'aliases'})
            if not isinstance(details,dict):continue
            aliases=[a.get('name','') for a in (details.get('aliases') or []) if isinstance(a,dict)]
        if not direct and not any(normalized_identity(a)==q for a in aliases):
            continue
        verified.append({'id':candidate['id'].lower(),
            'canonical':candidate.get('name') or query,
            'display':candidate.get('name') if direct else query,
            'aliases':aliases, 'score':score,'direct':direct})
    if not verified:
        return None
    verified.sort(key=lambda x:(x['direct'],x['score']),reverse=True)
    # Name or alias collisions across real artists are NOT resolved by a
    # search-engine score. A unique canonical match takes precedence; if
    # multiple candidates have equal identity evidence, require clarification.
    directs=[x for x in verified if x['direct']]
    if len(directs)==1:return directs[0]
    if len(directs)>1 or len(verified)>1:return None
    return verified[0]


async def album_groups_for_artist(d, client, identity):
    """Complete browsed external studio-album/EP discography, not Navidrome."""
    groups=await d._browse_artist_album_groups(client, identity['id'],max_groups=200)
    unique={}
    for g in groups:
        mbid=str(g.get('musicbrainz_id') or '')
        name=g.get('title') or ''
        if not d.MBID.fullmatch(mbid) or not name:continue
        key=d.normalized(name)
        year=str(g.get('year') or '')[:4]
        candidate={**g,'year':year,'title':name}
        previous=unique.get(key)
        if previous is None or ((year or '9999') < (str(previous.get('year') or '') or '9999')):
            unique[key]=candidate
    return sorted(unique.values(),key=lambda x:(str(x.get('year') or '0000'),d.normalized(x['title'])),reverse=True)


async def _songs_from_dn(d,client,username,identity,groups):
    async def tracks(group):
        raw=await d.dn_json(client,username,'tracks',album_mbid=group['musicbrainz_id'])
        if not isinstance(raw,dict):return []
        result=[]
        for item in (raw.get('tracks') or [])[:60]:
            if not isinstance(item,dict) or not d.MBID.fullmatch(str(item.get('recording_id') or '')):continue
            title=item.get('title')
            if not title:continue
            song=d.add_candidate(item['recording_id'],identity['canonical'],title,group['title'],
                int((item.get('length') or 0)/1000),
                release_mbid=raw.get('selected_release_mbid') or '',
                release_group_mbid=group['musicbrainz_id'])
            if song:result.append(song)
        return result
    batches=await asyncio.gather(*(tracks(g) for g in groups[:3]))
    # Mix previews across release groups so five search songs are not all
    # simply the first five tracks on one album.
    mixed=[]
    for index in range(max((len(batch) for batch in batches),default=0)):
        for batch in batches:
            if index<len(batch):mixed.append(batch[index])
            if len(mixed)>=30:return mixed
    return mixed


async def _songs_from_mb(d,client,identity,groups):
    # Metadata-only fallback when DroppedNeedle doesn't index those albums.
    # A valid release MBID is retained for exact-edition requests.
    songs=[]
    for group in groups[:2]:
        data=await d._artist_mb_get(client,'release/',
            {'release-group':group['musicbrainz_id'],'limit':12})
        if not isinstance(data,dict):continue
        editions=[x for x in (data.get('releases') or []) if isinstance(x,dict)
            and d.MBID.fullmatch(str(x.get('id') or '')) and x.get('status')=='Official']
        if not editions:continue
        editions.sort(key=lambda x:(str(x.get('date') or '9999'),str(x.get('id') or '')))
        edition=editions[0]
        details=await d._artist_mb_get(client,'release/'+edition['id'],
            {'inc':'recordings+artist-credits'})
        if not isinstance(details,dict):continue
        for medium in (details.get('media') or []):
            if not isinstance(medium,dict):continue
            for track in (medium.get('tracks') or []):
                if not isinstance(track,dict):continue
                rec=track.get('recording') or {}
                if not isinstance(rec,dict):continue
                title=track.get('title') or rec.get('title')
                if not title or not d.MBID.fullmatch(str(rec.get('id') or '')):continue
                song=d.add_candidate(rec['id'],identity['canonical'],title,group['title'],
                    int((rec.get('length') or track.get('length') or 0)/1000),
                    release_mbid=edition['id'],release_group_mbid=group['musicbrainz_id'])
                if song:songs.append(song)
                if len(songs)>=25:return songs
    return songs


def best_song_previews(d, songs, limit=MAX_SEARCH_SONGS):
    seen=set();result=[]
    for song in songs:
        key=d.normalized(song.get('title'))
        if not key or key in seen:continue
        # Suppress intro/skit results before limiting to five.
        if key in {'intro','outro','interlude','skit'} or key.startswith('skit '):continue
        seen.add(key);result.append(song)
        if len(result)>=limit:return result
    return result


async def artist_search(d,client,username,query):
    identity=await resolve_exact_artist(d,client,query)
    if not identity:return None
    from music_search_upgrade import popular_for_artist, top_albums, top_songs
    groups,popularity=await asyncio.gather(
        album_groups_for_artist(d,client,identity),
        popular_for_artist(client,identity['id']))
    # Popularity controls the search previews only, not the full artist page.
    popular_groups=top_albums(d,groups,(popularity or {}).get('groups',[]))
    # Store one canonical identity regardless of which alias was searched.
    # This keeps album IDs and song.albumId consistent across repeated searches
    # for former/current names, while the artist card may show the entered alias.
    canonical=identity['canonical']
    artist=d.remember_remote_artist(canonical,identity['id'],album_count=len(groups))
    if not artist:return None
    artist={**artist,'name':identity['display']}
    albums=[]
    for group in popular_groups:
        album=d.remember_remote_album(group['title'],canonical,
            mbid=group['musicbrainz_id'],artist_mbid=identity['id'],year=group.get('year'))
        if album:
            albums.append(album)
    # ListenBrainz top-recordings-for-artist is already sorted by actual
    # listens, not by a title containing the artist name. Keep a wider pool
    # so native-library duplicates don't consume the five EXTERNAL slots.
    songs=top_songs(d,identity,(popularity or {}).get('recordings',[]),limit=15)
    if len(songs)<5:
        # Fallback when LB is unavailable/has sparse data. The scoped MBID
        # catalog is still verified; never use tribute-band string matches.
        sampled=popular_groups[:3]
        extra=await _songs_from_dn(d,client,username,identity,sampled)
        if not extra:
            try:
                extra=await asyncio.wait_for(_songs_from_mb(d,client,identity,sampled),timeout=3.5)
            except asyncio.TimeoutError:
                extra=[]
        songs=best_song_previews(d,songs+extra,limit=15)
    return (songs,[artist],albums[:MAX_SEARCH_ALBUMS],identity)


# CASSETTE_SMART_SEARCH_V5: use verified fuzzy alias and finish artist card metadata.
_original_resolve_exact_artist_v4 = resolve_exact_artist
_original_artist_search_v4 = artist_search

async def resolve_exact_artist(d,client,query):
    exact=await _original_resolve_exact_artist_v4(d,client,query)
    if exact is not None:return exact
    from artist_enrichment_v5 import fuzzy_identity
    return await fuzzy_identity(d,client,query)

async def artist_search(d,client,username,query):
    result=await _original_artist_search_v4(d,client,username,query)
    from artist_enrichment_v5 import tidy_identity_card
    return tidy_identity_card(d,result)
