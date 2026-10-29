"""Regression tests for Stage 5 album favorite hotfix."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from starlette.datastructures import QueryParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cassette-proxy'))
import actions
import album_pages_stage5
import album_requests_v6


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['star', 'unstar'])
async def test_native_album_favorite_survives_missing_catalog(monkeypatch, action):
    events=[]

    class FakeClient:
        def build_request(self, method, url, **kwargs):
            events.append(('upstream_request', method, url))
            return httpx.Request(method, url, **kwargs)

        async def send(self, req, stream=False):
            return httpx.Response(200, request=req, json={
                'subsonic-response': {'status':'ok','version':'1.16.1'}})

    async def no_catalog(*args, **kwargs):
        return None

    async def fake_change(request, upstream, auth, kind, saved):
        events.append(('collection_change', kind, saved))

    import collection
    monkeypatch.setattr(album_requests_v6,'partial_native_album',no_catalog)
    monkeypatch.setattr(collection,'change',fake_change)
    params={'u':'Julia','p':'secret','f':'json','albumId':'native-123'}
    query=urlencode(params)
    request=SimpleNamespace(method='GET',headers={},query_params=QueryParams(query),
        scope={'query_string':query.encode()},
        app=SimpleNamespace(state=SimpleNamespace(client=FakeClient(),
                                                  star_lock=__import__('asyncio').Lock(),
                                                  personal_cache={})))
    response=await actions.handle_favorite(request,'http://nav',action+'.view',b'',
                                            lambda headers:list(headers.items()))
    assert response.status_code==200
    assert ('collection_change',action,[('albumId','native-123')]) in events
    assert any(e[0]=='upstream_request' for e in events)


@pytest.mark.asyncio
async def test_home_album_shows_user_starred_from_bridge(monkeypatch):
    import discovery
    from contextlib import contextmanager
    import sqlite3

    group='11111111-1111-1111-1111-111111111111'
    @contextmanager
    def cached():
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE discovery_albums (mbid TEXT,name TEXT,artist TEXT)')
        db.execute('INSERT INTO discovery_albums VALUES (?,?,?)',(group,'Demo','Artist'))
        try:yield db
        finally:db.close()

    async def nav(*args, **kwargs):
        return {'album':{'id':'native-1','name':'Demo','artist':'Artist','song':[{'id':'s1','duration':23}]}}

    async def membership(*args, **kwargs):
        return [{'id':'s1'}],{}

    monkeypatch.setattr(discovery,'connect',cached)
    monkeypatch.setattr(album_pages_stage5,'nav_json',nav)
    monkeypatch.setattr(album_pages_stage5,'personal_membership',membership)
    monkeypatch.setattr(album_requests_v6,'album_starred',lambda user, g: '2026-10-29T00:00:00Z' if user=='Julia' and g==group else None)
    query=urlencode({'u':'Julia','p':'secret','f':'json','id': 'native-1','cassetteScope':'personal'})
    request=SimpleNamespace(method='GET',headers={},query_params=QueryParams(query),
        app=SimpleNamespace(state=SimpleNamespace(client=None)))
    response=await album_pages_stage5.native_album_detail(request,'http://nav',b'')
    album=json.loads(response.body)['subsonic-response']['album']
    assert album['starred']=='2026-10-29T00:00:00Z'
    assert album['songCount']==1
