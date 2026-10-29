"""Offline checks for saved favorites sync safety. Run with unittest in the proxy image."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cassette-proxy'))
import collection
import discovery
import saved_favorites
import album_requests_v6


class FavoritesSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = collection.DB
        collection.DB = os.path.join(self.temp.name, 'state.sqlite')
        collection.initialize()
        discovery.setup()
        saved_favorites.initialize()

    def tearDown(self):
        collection.DB = self.old_db
        self.temp.cleanup()

    def test_missing_saved_album_metadata_fails_closed(self):
        group = '11111111-1111-1111-1111-111111111111'
        with collection.connect() as db:
            db.execute('INSERT INTO external_album_favorites VALUES (?,?,1,?)',
                       ('julia', group, 1234))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(client=None)))
        async def fresh_nav(*args, **kwargs):
            return {'starred2': {'song': [], 'album': []}}
        with patch.object(saved_favorites, 'nav_json', new=fresh_nav):
            with self.assertRaisesRegex(ValueError, 'Saved album metadata unavailable'):
                asyncio.run(saved_favorites.merged_get_starred2(request, 'http://nav', {'u': 'Julia'}))

    def test_virtual_song_stars_are_user_scoped(self):
        song_id = 'cassette-request-track_demo'
        with collection.connect() as db:
            db.execute('''INSERT INTO discovery
                (id,recording_mbid,artist,title,duration,created) VALUES (?,?,?,?,?,?)''',
                (song_id, '22222222-2222-2222-2222-222222222222', 'Artist', 'Song', 60, 1))
        saved_favorites.save_song('Julia', song_id)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(client=None)))
        async def fresh_nav(*args, **kwargs):
            return {'starred2': {'song': [], 'album': []}}
        with patch.object(saved_favorites, 'nav_json', new=fresh_nav):
            julia = asyncio.run(saved_favorites.merged_get_starred2(
                request, 'http://nav', {'u': 'Julia'}))
            other = asyncio.run(saved_favorites.merged_get_starred2(
                request, 'http://nav', {'u': 'spammz'}))
        self.assertEqual([s['id'] for s in json.loads(julia.body)['subsonic-response']['starred2']['song']],
                         [song_id])
        self.assertEqual(json.loads(other.body)['subsonic-response']['starred2']['song'], [])

    def test_star_survives_catalog_failure(self):
        group = '11111111-1111-1111-1111-111111111111'
        album_id = 'cassette-request-album_test'
        with collection.connect() as db:
            db.execute('INSERT INTO discovery_albums (id,mbid,name,artist,created) VALUES (?,?,?,?,?)',
                       (album_id, group, 'Demo', 'Artist', 1))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            client=None, personal_cache={})))
        with patch.object(album_requests_v6, 'nav_json', new=AsyncMock(return_value={})), \
             patch.object(discovery, 'BRIDGE_KEY', 'configured'), \
             patch.object(discovery, 'dn_json', new=AsyncMock(side_effect=ValueError('offline'))):
            response = asyncio.run(album_requests_v6.star_external_album(
                request, 'http://nav', {'u': 'Julia'}, album_id))
        self.assertEqual(json.loads(response.body)['subsonic-response']['status'], 'ok')
        self.assertEqual(response.headers['X-Cassette-Request-Status'], 'needs_metadata')
        self.assertIsNotNone(album_requests_v6.album_starred('Julia', group))


if __name__ == '__main__':
    unittest.main()
