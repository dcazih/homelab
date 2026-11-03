"""Integration checks with isolated DB and mocked acquisition boundaries (no downloads)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from cryptography.fernet import Fernet
import httpx

_tmp = tempfile.TemporaryDirectory()
os.environ['PROFILE_DB'] = _tmp.name + '/profiles.db'
os.environ['PEER_SCHEDULER_DB'] = _tmp.name + '/scheduler.db'
os.environ['PROFILE_ADMIN_KEY'] = 'test-admin-' * 5
os.environ['PROFILE_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'profiles'))
import app as core
import sona_catalog as catalog

GROUP = '11111111-1111-4111-8111-111111111111'
TRACK = '22222222-2222-4222-8222-222222222222'
USER = {'id': 'test-user', 'username': 'test-user'}
META = {'title': 'Test Album', 'artist_name': 'Test Artist', 'tracks': [{'recording_id': TRACK, 'title': 'Test Song', 'position': 1}]}

class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        core.app.dependency_overrides[core.require_user] = lambda: USER
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url='http://test')

    async def asyncTearDown(self):
        core.app.dependency_overrides.clear()
        await self.client.aclose()

    async def test_auth_required(self):
        core.app.dependency_overrides.clear()
        response = await self.client.get('/v1/sona/search?q=test')
        self.assertEqual(response.status_code, 401)

    async def test_invalid_id_never_reaches_pipeline(self):
        with patch.object(catalog, 'album_metadata', new_callable=AsyncMock) as metadata:
            response = await self.client.post('/v1/sona/request', json={'kind':'album','id':'z'*36})
            self.assertEqual(response.status_code, 400)
            metadata.assert_not_called()

    async def test_request_uses_authenticated_user_and_verified_metadata(self):
        with patch.object(catalog, 'album_metadata', AsyncMock(return_value=META)), patch.object(core,'manual_request',AsyncMock(return_value={'status':'requested'})) as submit:
            response = await self.client.post('/v1/sona/request', json={'kind':'song','id':TRACK,'album_id':GROUP,'username':'someone-else','artist':'forged'})
            self.assertEqual(response.status_code,200)
            args = submit.call_args
            self.assertEqual(args.kwargs['user'], USER)
            self.assertEqual(args.args[0].artist,'Test Artist')
            self.assertEqual(args.args[0].recording_mbid,TRACK)

    async def test_wrong_album_recording_is_not_submitted(self):
        with patch.object(catalog,'album_metadata',AsyncMock(return_value={**META,'tracks':[]})), patch.object(core,'manual_request',AsyncMock()) as submit:
            response = await self.client.post('/v1/sona/request', json={'kind':'song','id':TRACK,'album_id':GROUP})
            self.assertEqual(response.status_code,422)
            submit.assert_not_called()

    async def test_album_submits_existing_album_pipeline(self):
        with patch.object(catalog,'album_metadata',AsyncMock(return_value=META)), patch.object(catalog.albums,'request_album',AsyncMock(return_value={'status':'requested'})) as submit:
            response = await self.client.post('/v1/sona/request', json={'kind':'album','id':GROUP})
            self.assertEqual(response.status_code,200)
            self.assertEqual(submit.call_args.args[0].expected_tracks,1)
            self.assertEqual(submit.call_args.args[0].username, USER['username'])

    async def test_download_complete_without_navidrome_stays_unavailable(self):
        with patch.object(catalog,'album_metadata',AsyncMock(return_value={**META,'in_library':True,'requested':True})), patch.object(catalog.albums,'find_album_tracks',AsyncMock(return_value=[])):
            response = await self.client.get('/v1/sona/albums/'+GROUP)
            self.assertEqual(response.status_code,200)
            data=response.json()
            self.assertIsNone(data['localAlbum'])
            self.assertIsNone(data['tracks'][0]['localSong'])

    async def test_verified_arrival_unlocks_album(self):
        song={'id':'local','albumId':'album-local','title':'Test Song','artist':'Test Artist','album':'Test Album','musicBrainzId':TRACK}
        local_album={'id':'album-local','name':'Test Album','song':[song]}
        async def sub(user,endpoint,params): return {'song':song} if endpoint=='getSong' else {'album':local_album}
        with patch.object(catalog,'album_metadata',AsyncMock(return_value=META)), patch.object(catalog.albums,'find_album_tracks',AsyncMock(return_value=['local'])), patch.object(core,'sub',sub):
            data=(await self.client.get('/v1/sona/albums/'+GROUP)).json()
            self.assertEqual(data['localAlbum']['id'],'album-local')
            self.assertEqual(data['tracks'][0]['localSong']['id'],'local')

    async def test_different_recording_never_unlocks_by_title(self):
        wrong={'id':'wrong','title':'Test Song','artist':'Test Artist','album':'Test Album','musicBrainzId':GROUP}
        self.assertIsNone(catalog.match_song(META['tracks'][0],[wrong],'Test Artist','Test Album'))

    async def test_artist_discography_follows_browse_pagination(self):
        async def mb(endpoint, params):
            offset = params.get('offset', 0)
            return {'release-group-count': 103, 'release-groups': [
                {'id': str(i), 'title': 'Release ' + str(i), 'primary-type': 'Album'}
                for i in range(offset, min(offset + 100, 103))]}
        async def metadata(user, path, params=None):
            return {'songs': []} if path.endswith('top-songs') else {'name': 'Test Artist'}
        with patch.object(catalog.editions, 'mb_get', side_effect=mb) as browse, patch.object(catalog, 'metadata', side_effect=metadata):
            data = (await self.client.get('/v1/sona/artists/' + GROUP)).json()
            self.assertEqual(len(data['albums']), 103)
            self.assertEqual(browse.call_args_list[1].args[1]['offset'], 100)

class SearchRelevanceTests(unittest.IsolatedAsyncioTestCase):
    def test_conservative_spelling_prefix_and_word_overlap(self):
        from sona_search import affinity, paired_title
        self.assertEqual(affinity('miceal jackson','Michael Jackson'),87)
        self.assertEqual(affinity('Michael Jack','Michael Jackson'),91)
        self.assertEqual(affinity('MIKE','Mike Oldfield'),0)
        self.assertEqual(affinity('Jackson','Janet Jackson'),0)
        self.assertEqual(paired_title('MIKE Pompeii','POMPEII // UTILITY'),('mike',102))
        self.assertEqual(paired_title('MIKE Pompeii','Shake It Off / Pompeii'),('',0))

    def test_deduplicates_editions_without_merging_different_artist_ids(self):
        from sona_search import unique
        rows=[{'id':GROUP,'title':'Same','artist':'Same'}, {'id':TRACK,'title':'Same','artist':'Same'}]
        self.assertEqual(len(unique(rows,'album')),1)
        self.assertEqual(len(unique(rows,'artist')),2)

    async def test_alias_requires_real_artist_details(self):
        import sona_search as search
        async def metadata(path,params):
            if path=='artist':return {'artists':[{'id':GROUP,'name':'Ye','score':100}]}
            return {'id':GROUP,'name':'Ye','aliases':[{'name':'Kanye West'}]}
        with patch.object(search.mb,'mb_get',side_effect=metadata):
            result=await search.artist_candidates('Kanye West')
            self.assertEqual(result[0]['id'],GROUP)
            self.assertIn('Kanye West',result[0]['_names'])

    async def test_same_query_endpoints_coalesce_and_warm_cache(self):
        import sona_search as search
        search._cache.clear();search._tasks.clear()
        run=AsyncMock(return_value={'artists':[],'albums':[],'songs':[],'intent':'song'})
        with patch.object(search,'_run',run):
            await __import__('asyncio').gather(search.search(USER,'Billie Jean',None,None),search.search(USER,'Billie Jean',None,None))
            await search.search(USER,'Billie Jean',None,None)
            self.assertEqual(run.await_count,1)

class FastSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_artist_prefix_and_title_intent(self):
        from sona_search import hint_intent
        hints=[{'title':'One','artist':'Kendrick Lamar','album':'A'},{'title':'Two','artist':'Kendrick Lamar','album':'B'}]
        self.assertEqual(hint_intent('Kendrick',hints)[:2],('artist','Kendrick Lamar'))
        self.assertEqual(hint_intent('All Caps',[{'title':'All Caps','artist':'Madvillain','album':'Madvillainy'}])[:2],('song','All Caps'))
        self.assertEqual(hint_intent('Jay-Z Reasonable Doubt',[{'title':'Something','artist':'Jay-Z','album':'Reasonable Doubt (Fireworks Supermix)'}])[:2],('album','reasonable doubt'))

    async def test_candidate_retrieval_never_waits_for_request_metadata(self):
        import sona_search as search
        with patch.object(search,'relevance_hints',AsyncMock(return_value=[{'title':'Test Song','artist':'Test Artist','album':'Test Album'}])), patch.object(search.mb,'mb_get',AsyncMock(return_value={'recordings':[{'id':TRACK,'title':'Test Song','artist-credit':[{'artist':{'id':GROUP,'name':'Test Artist'}}],'releases':[{'title':'Test Album','status':'Official','release-group':{'id':GROUP,'primary-type':'Album'}}]}]})):
            verify=AsyncMock(side_effect=AssertionError('verification must not run during retrieval'))
            data=await search._run(USER,'Test Song',None,verify)
            self.assertEqual(len(data['songs']),1)
            self.assertFalse(data['songs'][0]['requestable'])
            self.assertTrue(data['songs'][0]['verificationPending'])
            verify.assert_not_called()

    async def test_separate_verification_rejects_unknown_recording(self):
        from sona_search import verify
        data=await verify(USER,'song',GROUP,GROUP,AsyncMock(return_value=META))
        self.assertFalse(data['requestable'])
        data=await verify(USER,'song',TRACK,GROUP,AsyncMock(return_value=META))
        self.assertTrue(data['requestable'])

    async def test_disconnected_queries_stop_planning(self):
        import asyncio
        import sona_search as search
        search._cache.clear();search._tasks.clear()
        started=asyncio.Event();cancelled=asyncio.Event()
        async def run(*args):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        request=type('Request',(),{'is_disconnected':AsyncMock(return_value=True)})()
        with patch.object(search,'_run',run):
            with self.assertRaises(asyncio.CancelledError):await search.search(USER,'obsolete query',None,None,request=request)
            await asyncio.wait_for(cancelled.wait(),1)

if __name__ == '__main__': unittest.main()
