"""Bridge authentication and saved layout smoke tests with isolated SQLite data."""
import importlib
import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

sys.path.insert(0, '/app')

class ProfileAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ['PROFILE_DB'] = os.path.join(cls.tmp.name, 'profiles.db')
        os.environ['PEER_SCHEDULER_DB'] = os.path.join(cls.tmp.name, 'scheduler.db')
        os.environ['PROFILE_ADMIN_KEY'] = 'a' * 40
        os.environ['PROFILE_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        cls.module = importlib.import_module('app')
        cls.client = TestClient(cls.module.app)
        cls.module.sub = AsyncMock(return_value={'status':'ok'})
        cls.user_id = secrets.token_hex(8)
        with cls.module.connection() as db:
            db.execute('INSERT INTO users VALUES(?,?,?,?,?)',
                       (cls.user_id,'alice',cls.module.cipher.encrypt(b'old-password'),
                        cls.module.digest('old-token'),1))

    def setUp(self):
        with self.module.connection() as db:
            db.execute('DELETE FROM profile_sessions')
            db.execute('DELETE FROM shared_track_followers')
            db.execute('DELETE FROM shared_track_acquisitions')
            db.execute('DELETE FROM proposals')
            db.execute('DELETE FROM cassette_album_requests')
            db.execute('DELETE FROM users WHERE id!=?',(self.user_id,))
            db.execute('DELETE FROM profile_layouts')
            db.execute('UPDATE users SET token_digest=?,secret=? WHERE id=?',
                       (self.module.digest('old-token'),self.module.cipher.encrypt(b'old-password'),self.user_id))
        self.module.sub.reset_mock()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_login_persists_layout_and_logout_revokes(self):
        login = self.client.post('/v1/auth/login',json={
            'navidrome_username':'alice','navidrome_password':'correct'} )
        self.assertEqual(login.status_code,200,login.text)
        token=login.json()['profile_token']
        headers={'Authorization':'Bearer '+token}
        self.assertEqual(self.client.get('/v1/me/layout',headers={'Authorization':'Bearer old-token'}).status_code,200)
        self.assertEqual(self.client.get('/v1/me/layout',headers=headers).json()['order'],[])
        self.assertEqual(self.client.put('/v1/me/layout',headers=headers,json={'order':['pending','upload']}).status_code,200)
        self.assertEqual(self.client.get('/v1/me/layout',headers=headers).json()['order'],['pending','upload'])
        self.assertEqual(self.client.post('/v1/auth/logout',headers=headers).status_code,200)
        self.assertEqual(self.client.get('/v1/me/layout',headers=headers).status_code,401)
        self.assertEqual(self.client.get('/v1/me/layout',headers={'Authorization':'Bearer old-token'}).status_code,200)

    def test_admin_key_accounts_and_layout(self):
        self.assertEqual(self.client.post('/v1/admin/login',json={'key':'wrong'}).status_code,401)
        self.assertEqual(self.client.get('/v1/admin/accounts').status_code,401)
        h={'X-Admin-Key':'a'*40}
        self.assertEqual(self.client.post('/v1/admin/login',json={'key':'a'*40}).status_code,200)
        self.assertEqual(self.client.get('/v1/admin/accounts',headers=h).json()['accounts'][0]['username'],'alice')
        self.assertEqual(self.client.put('/v1/admin/layout',headers=h,json={'order':['active-scheduler','pending']}).status_code,200)
        self.assertEqual(self.client.get('/v1/admin/layout',headers=h).json()['order'],['active-scheduler','pending'])
        self.assertEqual(self.client.put('/v1/admin/layout',headers=h,json={'order':['unknown']}).status_code,400)

    def test_clear_cancels_active_tracks_and_albums_before_hiding(self):
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES (?,?,?,?,?,?,?)",('t1',self.user_id,'Artist','Running','requested','11111111-1111-1111-1111-111111111111',1))
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,created) VALUES (?,?,?,?,?,?)",('t2',self.user_id,'Artist','Finished','added',1))
            db.execute("INSERT INTO cassette_album_requests(user_id,release_group_mbid,artist,album,expected_tracks,state,created,updated) VALUES (?,?,?,?,?,?,?,?)",(self.user_id,'22222222-2222-2222-2222-222222222222','Artist','Album',10,'awaiting_approval',1,1))
        cancel=AsyncMock()
        with patch.object(self.module,'_cancel_native_request',cancel):
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json(),{'cleared':3,'remaining':[]})
        self.assertEqual(cancel.await_count,2)
        with self.module.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM proposals WHERE id='t1'").fetchone()['state'],'cancelled')
            album=db.execute('SELECT state,save_desired,dismissed_at FROM cassette_album_requests').fetchone()
            self.assertEqual((album['state'],album['save_desired']),('cancelled',0))
            self.assertIsNotNone(album['dismissed_at'])

    def test_clear_preserves_failed_cancellation_and_shared_listener(self):
        with self.module.connection() as db:
            db.execute('INSERT INTO users VALUES(?,?,?,?,?)',('bob-id','bob',self.module.cipher.encrypt(b'pw'),self.module.digest('bob-token'),1))
            for pid,uid in [('owner',self.user_id),('follower','bob-id')]:
                db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES (?,?,?,?,?,?,?)",(pid,uid,'Artist','Shared','requested','33333333-3333-3333-3333-333333333333',1))
            db.execute("INSERT INTO shared_track_acquisitions VALUES (?,?,?,'accepted',?,?,?)",('33333333-3333-3333-3333-333333333333','','owner','task',1,1))
            db.execute("INSERT INTO shared_track_followers VALUES (?,?,?,?,?)",('follower','proposal','33333333-3333-3333-3333-333333333333','',1))
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES (?,?,?,?,?,?,?)",('failure',self.user_id,'Artist','Cannot Cancel','requested','44444444-4444-4444-4444-444444444444',1))
        cancel=AsyncMock(side_effect=ValueError('Cancellation unavailable'))
        with patch.object(self.module,'_cancel_native_request',cancel):
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={})
        self.assertEqual(result.json()['cleared'],1)
        self.assertEqual(result.json()['remaining'][0]['title'],'Cannot Cancel')
        self.assertEqual(cancel.await_count,1)
        with self.module.connection() as db:
            self.assertIsNone(db.execute("SELECT dismissed_at FROM proposals WHERE id='failure'").fetchone()['dismissed_at'])
            self.assertIsNone(db.execute("SELECT dismissed_at FROM proposals WHERE id='follower'").fetchone()['dismissed_at'])
            self.assertEqual(db.execute('SELECT state FROM shared_track_acquisitions').fetchone()['state'],'accepted')

    def test_clear_shared_album_preserves_other_person(self):
        group='55555555-5555-5555-5555-555555555555'
        with self.module.connection() as db:
            db.execute('INSERT INTO users VALUES(?,?,?,?,?)',('bob-id','bob',self.module.cipher.encrypt(b'pw'),self.module.digest('bob-token'),1))
            for uid in (self.user_id,'bob-id'):
                db.execute("INSERT INTO cassette_album_requests(user_id,release_group_mbid,artist,album,expected_tracks,state,created,updated) VALUES(?,?,?,?,?,?,?,?)",(uid,group,'Artist','Shared Album',10,'requested',1,1))
        cancel=AsyncMock()
        with patch.object(self.module,'_cancel_native_request',cancel):
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={})
        self.assertEqual(result.json(),{'cleared':1,'remaining':[]})
        cancel.assert_not_awaited()
        with self.module.connection() as db:
            self.assertEqual(db.execute("SELECT save_desired FROM cassette_album_requests WHERE user_id='bob-id'").fetchone()['save_desired'],1)

    def test_clear_uses_native_request_cancel_endpoint(self):
        mbid='66666666-6666-6666-6666-666666666666'
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES(?,?,?,?,?,?,?)",('native',self.user_id,'Artist','Track','requested',mbid,1))
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'success':True,'message':'Cancelled'})
        client_class=httpx.AsyncClient
        with patch.object(self.module,'linked_token',return_value='test-dn-token'), \
             patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={})
        self.assertEqual(result.json(),{'cleared':1,'remaining':[]})
        self.assertEqual(calls[0].method,'DELETE')
        self.assertEqual(calls[0].url.path,'/api/v1/requests/active/'+mbid)
        self.assertEqual(calls[0].url.params['request_kind'],'track')

    def test_clear_imported_album_with_stale_bridge_state(self):
        mbid='77777777-7777-7777-7777-777777777777'
        with self.module.connection() as db:
            db.execute("INSERT INTO cassette_album_requests(user_id,release_group_mbid,artist,album,expected_tracks,state,created,updated) VALUES(?,?,?,?,?,?,?,?)",(self.user_id,mbid,'Artist','Album',10,'awaiting_approval',1,1))
        def handler(request):
            return httpx.Response(200,json={'success':False,'message':"Cannot cancel request with status 'imported'"})
        client_class=httpx.AsyncClient
        with patch.object(self.module,'linked_token',return_value='test-dn-token'), \
             patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={'album_ids':[mbid]})
        self.assertEqual(result.json(),{'cleared':1,'remaining':[]})
        with self.module.connection() as db:
            row=db.execute('SELECT dismissed_at,save_desired FROM cassette_album_requests WHERE user_id=? AND release_group_mbid=?',(self.user_id,mbid)).fetchone()
            self.assertIsNotNone(row['dismissed_at'])
            self.assertEqual(row['save_desired'],0)

    def test_clear_one_track_leaves_other_requests(self):
        with self.module.connection() as db:
            for item in ('one','two'):
                db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES(?,?,?,?,?,?,?)",(item,self.user_id,'Artist',item,'requested','66666666-6666-6666-6666-666666666666',1))
            db.execute("INSERT INTO cassette_album_requests(user_id,release_group_mbid,artist,album,expected_tracks,state,created,updated) VALUES(?,?,?,?,?,?,?,?)",(self.user_id,'77777777-7777-7777-7777-777777777777','Artist','Album',10,'ready',1,1))
        with patch.object(self.module,'_cancel_native_request',AsyncMock()) as cancel:
            result=self.client.post('/v1/requests/clear',headers={'Authorization':'Bearer old-token'},json={'proposal_ids':['one']})
        self.assertEqual(result.json(),{'cleared':1,'remaining':[]})
        cancel.assert_awaited_once()
        with self.module.connection() as db:
            self.assertIsNone(db.execute("SELECT dismissed_at FROM proposals WHERE id='two'").fetchone()['dismissed_at'])
            self.assertIsNone(db.execute('SELECT dismissed_at FROM cassette_album_requests').fetchone()['dismissed_at'])

    def test_manual_song_requests_use_existing_approval(self):
        with patch.object(self.module,'approve',AsyncMock(return_value={'status':'requested','message':'Accepted'})) as approve:
            response=self.client.post('/v1/requests/manual',headers={'Authorization':'Bearer old-token'},json={'kind':'song','artist':'Artist','title':'Song','album':'Edition'})
        self.assertEqual(response.status_code,200,response.text)
        approve.assert_awaited_once()
        with self.module.connection() as db:
            self.assertEqual(db.execute("SELECT album FROM proposals WHERE title='Song'").fetchone()['album'],'Edition')

    def test_manual_album_requires_unambiguous_verified_metadata(self):
        import edition_matching
        with patch.object(edition_matching,'mb_get',AsyncMock(return_value={'release-groups':[]})):
            response=self.client.post('/v1/requests/manual',headers={'Authorization':'Bearer old-token'},json={'kind':'album','artist':'Artist','title':'Album'})
        self.assertEqual(response.status_code,400)
        self.assertIn('one exact album',response.json()['detail'])
        with self.module.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cassette_album_requests').fetchone()[0],0)

    def test_manual_album_submits_verified_release_group(self):
        import edition_matching,album_request_bridge_v6
        gid='11111111-1111-1111-1111-111111111111';rid='22222222-2222-2222-2222-222222222222'
        group={'id':gid,'title':'Album','primary-type':'Album','artist-credit':[{'artist':{'name':'Artist'}}]}
        async def metadata(path,params):
            if path=='release-group':return {'release-groups':[group]}
            if path=='release':return {'releases':[{'id':rid,'title':'Album','status':'Official'}]}
            return {'media':[{'track-count':12}]}
        with patch.object(edition_matching,'mb_get',side_effect=metadata), patch.object(album_request_bridge_v6,'request_album',AsyncMock(return_value={'status':'requested'})) as request:
            response=self.client.post('/v1/requests/manual',headers={'Authorization':'Bearer old-token'},json={'kind':'album','artist':'Artist','title':'Album'})
        self.assertEqual(response.status_code,200,response.text)
        payload=request.await_args.args[0];self.assertEqual((payload.release_group_mbid,payload.expected_tracks),(gid,12))

    def test_recording_options_require_verified_matches(self):
        import edition_matching,metadata_resolver_v1
        rid='33333333-3333-3333-3333-333333333333'
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,created) VALUES(?,?,?,?,?,?)",('ambiguous',self.user_id,'Artist','Song','needs_metadata',1))
        search={'recordings':[{'id':rid,'title':'Song','disambiguation':'explicit','artist-credit':[{'artist':{'name':'Artist'}}]}]}
        verified=AsyncMock(return_value=[(rid,'release','group','Album')])
        with patch.object(edition_matching,'mb_get',AsyncMock(return_value=search)),patch.object(metadata_resolver_v1,'verified_recording_release_pairs',verified):
            response=self.client.get('/v1/proposals/ambiguous/recording-options',headers={'Authorization':'Bearer old-token'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['options'],[{'recording_mbid':rid,'version':'explicit','album':'Album'}])

    def test_default_prefers_explicit_verified_recording(self):
        import metadata_resolver_v1
        clean='11111111-1111-1111-1111-111111111111';explicit='22222222-2222-2222-2222-222222222222'
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,album,state,created) VALUES(?,?,?,?,?,?,?)",('pick',self.user_id,'Artist','Song','','needs_metadata',1))
            user=dict(db.execute('SELECT * FROM users WHERE id=?',(self.user_id,)).fetchone())
            proposal=dict(db.execute("SELECT * FROM proposals WHERE id='pick'").fetchone())
        options={'options':[{'recording_mbid':clean,'version':'clean','album':'Album'},{'recording_mbid':explicit,'version':'explicit','album':'Album'}]}
        pairs=[(explicit,'33333333-3333-3333-3333-333333333333','44444444-4444-4444-4444-444444444444','Album')]
        client_class=httpx.AsyncClient
        def handler(request):return httpx.Response(200,json={'status':'queued','task_id':'test'})
        with patch.object(self.module,'linked_token',return_value='token'),patch.object(self.module,'proposal_recording_options',AsyncMock(return_value=options)),patch.object(metadata_resolver_v1,'verified_recording_release_pairs',AsyncMock(return_value=pairs)),patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            import asyncio
            result=asyncio.run(self.module._submit_verified_cassette_track(user,'pick',proposal))
        self.assertEqual(result['status'],'requested')
        with self.module.connection() as db:
            self.assertEqual(db.execute("SELECT recording_mbid FROM proposals WHERE id='pick'").fetchone()['recording_mbid'],explicit)

    def test_live_status_uses_own_requested_recording_and_preserves_review(self):
        mbid='66666666-6666-6666-6666-666666666666'
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES(?,?,?,?,?,?,?)",('live',self.user_id,'Artist','Track','requested',mbid,1))
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,created) VALUES(?,?,?,?,?,?)",('review',self.user_id,'Artist','Review','needs_metadata',2))
        def handler(request):
            return httpx.Response(200,json={'items':[{'musicbrainz_id':mbid,'request_kind':'track','status':'downloading','progress':37}] if request.url.path.endswith('/active') else []})
        client_class=httpx.AsyncClient
        with patch.object(self.module,'linked_token',return_value='token'),patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            response=self.client.get('/v1/requests/live-status',headers={'Authorization':'Bearer old-token'})
        self.assertEqual(response.status_code,200,response.text)
        entries={item['id']:item['text'] for item in response.json()['requests']}
        self.assertEqual(entries['live'],'Downloading • 37%')
        self.assertTrue(entries['review'].startswith('Needs review'))

    def test_download_events_translate_and_stream_for_owned_requests(self):
        import asyncio,json
        from contextlib import asynccontextmanager
        mbid='66666666-6666-6666-6666-666666666666'
        with self.module.connection() as db:
            db.execute("INSERT INTO proposals(id,user_id,artist,title,state,recording_mbid,created) VALUES(?,?,?,?,?,?,?)",('event-song',self.user_id,'Artist','Track','requested',mbid,1))
            user=dict(db.execute('SELECT * FROM users WHERE id=?',(self.user_id,)).fetchone())
        class FakeResponse:
            def raise_for_status(self):pass
            def json(self):return {'items':[{'id':'task-1','download_type':'track','recording_mbid':mbid,'status':'searching'}]}
            async def aiter_lines(self):
                for line in ('event: progress','data: {"progress_percent":37}',''):
                    yield line
        class FakeClient:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,*args,**kwargs):return FakeResponse()
            @asynccontextmanager
            async def stream(self,*args,**kwargs):yield FakeResponse()
        async def run():
            with patch.object(self.module,'linked_token',return_value='token'),patch.object(self.module.httpx,'AsyncClient',return_value=FakeClient()):
                response=await self.module.live_request_events(user=user)
                iterator=response.body_iterator
                self.assertIn('connected',await anext(iterator))
                initial=await anext(iterator);self.assertEqual(json.loads(initial[6:])['text'],'Searching')
                update=await asyncio.wait_for(anext(iterator),1);self.assertEqual(json.loads(update[6:])['text'],'Downloading • 37%')
                await iterator.aclose()
        asyncio.run(run())
        self.assertEqual(self.module._download_event_text('status',{'status':'retrying'}),'Switching source')
        self.assertEqual(self.module._download_event_text('complete',{'status':'failed','error':'No source'}),'Failed • No source')

    def test_request_and_alias_redirect_to_canonical_profiles(self):
        response=self.client.get('/requests',follow_redirects=False)
        self.assertEqual(response.status_code,307)
        self.assertEqual(response.headers['location'],'/')
        response=self.client.get('https://profile.hippocampus-polaris.ts.net/requests?source=test',follow_redirects=False)
        self.assertEqual(response.status_code,307)
        self.assertEqual(response.headers['location'],'https://profiles.hippocampus-polaris.ts.net/requests?source=test')

    def test_scheduler_retry_uses_linked_personal_account(self):
        from contextlib import contextmanager
        scheduler=importlib.import_module('peer_scheduler_api').scheduler
        class FakeDB:
            def execute(self,*args): return self
            def fetchone(self): return {'state':'failed'}
        @contextmanager
        def fake_connect(): yield FakeDB()
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'success':True,'task_id':'new-task'})
        client_class=httpx.AsyncClient
        with patch.object(scheduler,'connect',fake_connect), patch.object(scheduler,'mark'), \
             patch.object(self.module,'linked_token',return_value='linked-dn-session'), \
             patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            response=self.client.post('/v1/admin/scheduler/jobs/task/retry',headers={
                'X-Admin-Key':'a'*40,'Authorization':'Bearer old-token'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(requests[0].headers['Authorization'],'Bearer linked-dn-session')

    def test_reset_password_uses_native_navidrome_api(self):
        seen=[]
        def handler(request):
            seen.append(request)
            if request.url.path=='/auth/login':
                return httpx.Response(200,json={'token':'native-token','id':'nav-admin','isAdmin':True})
            if request.method=='GET' and request.url.path=='/api/user':
                return httpx.Response(200,json=[{'id':'nav-alice','userName':'alice','name':'Alice','isAdmin':False,'email':'keep@example.test'}])
            if request.method=='PUT' and request.url.path=='/api/user/nav-alice':
                return httpx.Response(200,json={})
            return httpx.Response(404)
        client_class=httpx.AsyncClient
        with patch.object(self.module.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            result=self.client.post('/v1/admin/accounts/'+self.user_id+'/reset-password',
                headers={'X-Admin-Key':'a'*40,'Authorization':'Bearer old-token'},json={'new_password':'fresh-password'})
        self.assertEqual(result.status_code,200,result.text)
        import json
        payload=json.loads(seen[-1].content)
        self.assertEqual(payload['email'],'keep@example.test')
        self.assertEqual(payload['password'],'fresh-password')
        self.assertEqual(self.client.get('/v1/me/layout',headers={'Authorization':'Bearer old-token'}).status_code,401)
        with self.module.connection() as db:
            saved=db.execute('SELECT secret FROM users WHERE id=?',(self.user_id,)).fetchone()
        self.assertEqual(self.module.cipher.decrypt(saved['secret']),b'fresh-password')

if __name__=='__main__': unittest.main()
