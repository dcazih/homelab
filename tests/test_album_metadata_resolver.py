import unittest
from album_metadata_resolver import fold, resolve_album

G1='11111111-1111-1111-1111-111111111111'
G2='22222222-2222-2222-2222-222222222222'
A1='33333333-3333-3333-3333-333333333333'


def group(gid, title, name, artist_id=A1, typ='Album', secondary=None):
    return {'id':gid, 'title':title, 'primary-type':typ,
            'secondary-types':secondary or [],
            'artist-credit':[{'name':name,'artist':{'id':artist_id,'name':name}}]}


class AlbumResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_accent_and_volume_normalization(self):
        self.assertEqual(fold('Piñata'),fold('Pinata'))
        self.assertEqual(fold('Thug Life, Volume I'),fold('Thug Life Vol. 1'))
        self.assertNotEqual(fold('Flygod'),fold('Flygod 2'))

    async def test_finds_artist_after_strict_search_misses(self):
        good=group(G1,'Reasonable Doubt','JAY-Z')
        queries=[]
        async def mb_get(path, params):
            queries.append(params['query'])
            if ' AND artist:' in params['query']:
                return {'release-groups': []}
            return {'release-groups':[good]}
        result=await resolve_album('Jay-Z','Reasonable Doubt',mb_get)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['group']['id'],G1)
        self.assertEqual(len(queries),2)

    async def test_artist_alias_on_verified_mbid(self):
        good=group(G1,'Pinata','Freddie Gibbs',A1)
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[good]}
            if path=='artist/'+A1:return {'name':'Freddie Gibbs','aliases':[{'name':'Freddie'}]}
            raise AssertionError(path)
        result=await resolve_album('Freddie','Piñata',mb_get)
        self.assertEqual(result['status'],'matched')

    async def test_accepts_mixtape(self):
        valid=group(G1,'The Plugs I Met','Benny the Butcher',typ='Other',secondary=['Mixtape/Street'])
        async def mb_get(path,params):return {'release-groups':[valid]}
        result=await resolve_album('Benny the Butcher','The Plugs I Met',mb_get)
        self.assertEqual(result['status'],'matched')

    async def test_refuses_multiple_distinct_groups(self):
        a=group(G1,'The Black Album','Jay-Z')
        b=group(G2,'The Black Album','Jay-Z')
        async def mb_get(path,params):return {'release-groups':[a,b]}
        result=await resolve_album('Jay-Z','The Black Album',mb_get)
        self.assertEqual(result['status'],'ambiguous')
        self.assertEqual(len(result['candidates']),2)

    async def test_does_not_misattribute_unrelated_artist(self):
        wrong=group(G1,'Madvillainy','Madvillain')
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[wrong]}
            if path=='artist/'+A1:return {'name':'Madvillain', 'aliases': []}
            raise AssertionError(path)
        result=await resolve_album('MF DOOM','Madvillainy',mb_get)
        self.assertEqual(result['status'],'not_found')
        self.assertEqual(result['candidates'][0]['id'],G1)

    async def test_mb_id_still_checks_identity(self):
        wrong=group(G1,'Wrong Record','Jay-Z')
        async def mb_get(path,params):return wrong
        result=await resolve_album('Jay-Z','The Blueprint',mb_get,G1)
        self.assertEqual(result['status'],'not_found')

    async def test_release_title_fallback(self):
        official=group(G1,'Canonical Name','Artist')
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[]}
            if path=='release':return {'releases':[{'title':'Requested Title','release-group':{'id':G1}}]}
            if path=='release-group/'+G1:return official
            raise AssertionError(path)
        result=await resolve_album('Artist','Requested Title',mb_get)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['group']['title'],'Canonical Name')


if __name__=='__main__':unittest.main()
