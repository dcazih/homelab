"""Offline regression tests: no running Navidrome or DroppedNeedle required.

Run: python -m pytest -q tests/test_stage5.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from cryptography.fernet import Fernet
from starlette.datastructures import QueryParams

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cassette-proxy'))
import personal
import virtual
import actions
import album_pages_stage5


def test_scoped_home_album_id_roundtrip():
    album = {'id':'native-a123','name':'The Album','artistId':'a1'}
    encoded=virtual.as_virtual_album(album)
    assert virtual.decode_id(encoded['id']) == ('album','native-a123')
    assert virtual.decode_id(encoded['artistId']) == ('artist','a1')
    assert virtual.decode_id('native-a123') is None


def test_display_playlist_names():
    assert personal.personal_playlist_name('Julia') == 'Julia’s Music'
    assert personal.personal_playlist_name('spammz') == 'spammz’s Music'
    assert personal.is_personal_playlist('My Music','Julia')
    assert personal.is_personal_playlist('Julia’s Music','Julia')
    assert not personal.is_personal_playlist('Julia’s Music','spammz')


@pytest.mark.asyncio
async def test_rename_keeps_playlist_id(monkeypatch):
    calls=[]
    async def fake_nav(client,upstream,method,auth,extra=None):
        calls.append((method,extra))
        if method=='getPlaylists':return {'playlists':{'playlist':[
            {'id':'pid-100','name':'My Music','owner':'Julia'}]}}
        if method=='getPlaylist':return {'playlist':{'id':'pid-100','owner':'Julia','entry':[]}}
        return {}
    monkeypatch.setattr(actions,'nav_json',fake_nav)
    pid,entries=await actions.playlist_for_user(None,'http://nav',{'u':'Julia'},True)
    assert pid=='pid-100'
    assert ('updatePlaylist',{'playlistId':'pid-100','name':'Julia’s Music','public':'false'}) in calls
    assert not any(name=='createPlaylist' for name,_ in calls)


@pytest.mark.asyncio
async def test_home_album_only_shows_personal_tracks(monkeypatch):
    source={'id':'n-album','name':'Demo','artist':'Artist','song':[
        {'id':'s1','duration':30},{'id':'s2','duration':30}]}
    async def fake_nav(*args,**kwargs):return {'status':'ok','album':source}
    async def member(*args,**kwargs):return ([{'id':'s2'}],{})
    monkeypatch.setattr(album_pages_stage5,'nav_json',fake_nav)
    monkeypatch.setattr(album_pages_stage5,'personal_membership',member)
    encoded=virtual.virtual_id('album','n-album')
    request=SimpleNamespace(method='GET',headers={},query_params=QueryParams(
        urlencode({'u':'Julia','p':'secret','f':'json','id':encoded})),
        app=SimpleNamespace(state=SimpleNamespace(client=None)))
    response=await album_pages_stage5.native_album_detail(request,'http://nav',b'')
    obj=json.loads(response.body)['subsonic-response']['album']
    assert obj['id']==encoded
    assert [s['id'] for s in obj['song']]==['s2']
    assert obj['songCount']==1
    assert response.headers['X-Cassette-Album-View']=='personal'


@pytest.mark.asyncio
async def test_catalog_native_without_remote_returns_real_full_local_album(monkeypatch):
    source={'id':'n-album','name':'Demo','artist':'Artist','song':[
        {'id':'s1','duration':30},{'id':'s2','duration':30}]}
    async def fake_nav(*args,**kwargs):return {'status':'ok','album':source}
    monkeypatch.setattr(album_pages_stage5,'nav_json',fake_nav)
    import discovery
    monkeypatch.setattr(discovery,'connect',lambda: __import__('contextlib').nullcontext(
        __import__('sqlite3').connect(':memory:')))
    # With a seeded in-memory table, the normal view has the native IDs.
    def scoped_connect():
        from contextlib import contextmanager
        @contextmanager
        def conn():
            db=__import__('sqlite3').connect(':memory:')
            db.execute('''CREATE TABLE discovery_albums
                (id TEXT,mbid TEXT,name TEXT,artist TEXT,artist_id TEXT,cover_id TEXT,year INTEGER,song_count INTEGER,created INTEGER)''')
            yield db
            db.close()
        return conn()
    monkeypatch.setattr(discovery,'connect',scoped_connect)
    import album_requests_v6
    async def unavailable(*args,**kwargs):return None
    monkeypatch.setattr(album_requests_v6,'partial_native_album',unavailable)
    request=SimpleNamespace(method='GET',headers={},query_params=QueryParams(
        urlencode({'u':'Julia','p':'secret','f':'json','id':'n-album'})),
        app=SimpleNamespace(state=SimpleNamespace(client=None)))
    response=await album_pages_stage5.native_album_detail(request,'http://nav',b'')
    obj=json.loads(response.body)['subsonic-response']['album']
    assert [s['id'] for s in obj['song']]==['s1','s2']
    assert response.headers['X-Cassette-Album-View']=='catalog-local-unverified'


def test_collection_individual_overrides_album_unstar(monkeypatch,tmp_path):
    import collection
    monkeypatch.setattr(collection,'DB',str(tmp_path/'collection.sqlite'))
    collection.initialize()
    with collection.connect() as db:
        db.executemany('INSERT INTO individual VALUES (?,?)',[
            ('julia','song1'),('julia','song2')])
        db.executemany('INSERT INTO album_songs VALUES (?,?,?)',[
            ('julia','album1','song1'),('julia','album1','song3')])
        db.execute('DELETE FROM album_songs WHERE username=? AND album=?',('julia','album1'))
        assert collection.desired(db,'julia')=={'song1','song2'}


def test_bridge_album_unstar_preserves_song_stars_and_other_album(tmp_path):
    """Exercise the real SQLite migration and unsave route in an isolated process."""
    import subprocess
    import textwrap
    script = r'''
import asyncio, time
import app as core
import album_request_bridge_v6 as albums
uid='uid';g1='11111111-1111-1111-1111-111111111111';g2='22222222-2222-2222-2222-222222222222'
with core.connection() as db:
    db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (uid,'Julia',b'secret','digest',int(time.time())))
    for g in [g1,g2]:
        db.execute('INSERT INTO cassette_album_requests (user_id,release_group_mbid,artist,album,expected_tracks,state,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                   (uid,g,'Artist','Demo',3,'ready',1,1))
    for sid in ['individual','album-only','shared']:
        db.execute('INSERT INTO cassette_album_track_links VALUES (?,?,?,1)',(uid,g1,sid))
    db.execute('INSERT INTO cassette_album_track_links VALUES (?,?,?,0)',(uid,g2,'shared'))
entries=[{'id':'individual'},{'id':'album-only'},{'id':'shared'}]
removed=[]
async def fake_sub(user,kind,params=None):
    if kind=='getStarred2':return {'starred2':{'song':[{'id':'individual'}]}}
    if kind=='getPlaylist':return {'playlist':{'entry':entries}}
    if kind=='updatePlaylist':
        removed.append(int(params['songIndexToRemove']));return {}
    raise RuntimeError(kind)
async def fake_playlist(user):return 'p1',{s['id'] for s in entries}
core.sub=fake_sub
core.playlist_ids=fake_playlist
result=asyncio.run(albums.unstar_album(albums.AlbumUnsave(username='Julia',release_group_mbid=g1)))
assert removed==[1],removed
assert result['removed_count']==1,result
with core.connection() as db:
    assert db.execute('SELECT save_desired FROM cassette_album_requests WHERE user_id=? AND release_group_mbid=?',(uid,g1)).fetchone()[0]==0
    assert db.execute('SELECT save_desired FROM cassette_album_requests WHERE user_id=? AND release_group_mbid=?',(uid,g2)).fetchone()[0]==1
'''
    env={**os.environ,'PROFILE_ADMIN_KEY':'A'*40,
         'PROFILE_ENCRYPTION_KEY':Fernet.generate_key().decode(),
         'PROFILE_DB':str(tmp_path/'bridge.sqlite'),
         'PYTHONPATH':str(ROOT/'profiles')}
    result=subprocess.run([sys.executable,'-c',script],env=env,
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
