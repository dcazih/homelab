import os
import sys
import unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'profiles')))
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
            if path=='artist':
                return {'artists':[{'id':A1,'name':'Freddie Gibbs'}]} if 'alias:' in params['query'] else {'artists':[]}
            if path=='artist/'+A1:return {'id':A1,'name':'Freddie Gibbs','aliases':[{'name':'Freddie'}],'relations':[]}
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
            if path=='artist':return {'artists': []}
            if path=='artist/'+A1:return {'id':A1, 'name':'Madvillain', 'aliases': [], 'relations': []}
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



    async def test_verified_member_group_madvillain(self):
        group_artist_id='44444444-4444-4444-4444-444444444444'
        artist_id='55555555-5555-5555-5555-555555555555'
        album=group(G1,'Madvillainy','Madvillain',group_artist_id)
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[album]}
            if path=='artist':return {'artists':[{'id':artist_id,'name':'MF DOOM'}]}
            if path=='artist/'+artist_id:
                return {'id':artist_id,'name':'MF DOOM','relations':[{
                    'type-id':'5be4c609-9afa-4ea0-910b-12ffb71e3821',
                    'direction':'forward','artist':{'id':group_artist_id,'name':'Madvillain'}}]}
            if path=='artist/'+group_artist_id:
                return {'id':group_artist_id,'name':'Madvillain','relations':[]}
            raise AssertionError(path)
        result=await resolve_album('MF DOOM','Madvillainy',mb_get)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['match_type'],'verified_group_membership')

    async def test_personas_share_verified_legal_person(self):
        artist_id='55555555-5555-5555-5555-555555555555'
        legal_id='66666666-6666-6666-6666-666666666666'
        persona_id='77777777-7777-7777-7777-777777777777'
        album=group(G1,'Vaudeville Villain','Viktor Vaughn',persona_id)
        persona_rel=lambda:{'type-id':'dd9886f2-1dfe-4270-97db-283f6839a666',
                            'direction':'backward','artist':{'id':legal_id,'name':'Daniel Dumile'}}
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[album]}
            if path=='artist':return {'artists':[{'id':artist_id,'name':'MF DOOM'}]}
            if path=='artist/'+artist_id:
                return {'id':artist_id,'name':'MF DOOM','relations':[persona_rel()]}
            if path=='artist/'+persona_id:
                return {'id':persona_id,'name':'Viktor Vaughn','relations':[persona_rel()]}
            raise AssertionError(path)
        result=await resolve_album('MF DOOM','Vaudeville Villain',mb_get)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['match_type'],'performance_name')

    async def test_unrelated_collaborator_not_promoted_to_album_artist(self):
        artist_id='55555555-5555-5555-5555-555555555555'
        group_artist_id='66666666-6666-6666-6666-666666666666'
        album=group(G1,'Unrelated','Different Artist',group_artist_id)
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[album]}
            if path=='artist':return {'artists':[{'id':artist_id,'name':'MF DOOM'}]}
            if path=='artist/'+artist_id:
                return {'id':artist_id,'name':'MF DOOM','relations':[{
                    'type-id':'75c09861-6857-4ec0-9729-84eefde7fc86',
                    'direction':'forward','artist':{'id':group_artist_id}}]}
            if path=='artist/'+group_artist_id:
                return {'id':group_artist_id,'name':'Different Artist','relations':[]}
            raise AssertionError(path)
        result=await resolve_album('MF DOOM','Unrelated',mb_get)
        self.assertEqual(result['status'],'not_found')

    async def test_never_guesses_between_two_member_groups(self):
        artist_id='55555555-5555-5555-5555-555555555555'
        group_artist_a='66666666-6666-6666-6666-666666666666'
        group_artist_b='77777777-7777-7777-7777-777777777777'
        albums=[group(G1,'Joint Album','Group A',group_artist_a),
                group(G2,'Joint Album','Group B',group_artist_b)]
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':albums}
            if path=='artist':return {'artists':[{'id':artist_id,'name':'Collaborator'}]}
            if path=='artist/'+artist_id:
                return {'id':artist_id,'name':'Collaborator','relations':[
                    {'type-id':'5be4c609-9afa-4ea0-910b-12ffb71e3821',
                     'direction':'forward','artist':{'id':a}}
                     for a in (group_artist_a,group_artist_b)]}
            return {'id':path.split('/')[-1],'relations':[]}
        result=await resolve_album('Collaborator','Joint Album',mb_get)
        self.assertEqual(result['status'],'ambiguous')
        self.assertEqual(len(result['candidates']),2)


    async def test_unclassified_release_group_can_be_preflight_checked(self):
        album=group(G1,'Strobelight','Boldy James',typ=None)
        async def mb_get(path,params):return {'release-groups':[album]}
        result=await resolve_album('Boldy James','Strobelight',mb_get)
        self.assertEqual(result['status'],'matched')

    async def test_title_extension_with_double_slash(self):
        album=group(G1,'POMPEII // UTILITY','MIKE')
        async def mb_get(path,params):return {'release-groups':[album]}
        result=await resolve_album('MIKE','Pompeii',mb_get)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['match_type'],
                         'artist_credit+canonical_title_extension')

    async def test_does_not_guess_arbitrary_album_title_prefix(self):
        album=group(G1,'The Blueprint 2','Jay-Z')
        async def mb_get(path,params):
            if path=='release-group':return {'release-groups':[album]}
            if path=='release':return {'releases':[]}
            raise AssertionError(path)
        result=await resolve_album('Jay-Z','The Blueprint',mb_get)
        self.assertEqual(result['status'],'not_found')


if __name__=='__main__':unittest.main()
