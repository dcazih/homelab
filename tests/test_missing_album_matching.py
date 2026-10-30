import asyncio
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, '/app')
import metadata_resolver_v1 as resolver
import edition_matching

RID='11111111-1111-1111-1111-111111111111'
RID2='22222222-2222-2222-2222-222222222222'
RELEASE='33333333-3333-3333-3333-333333333333'
GROUP='44444444-4444-4444-4444-444444444444'
CREDIT=[{'artist':{'name':'Larry June'}}]

class MissingAlbumTests(unittest.IsolatedAsyncioTestCase):
    async def test_verifies_recording_on_actual_album(self):
        async def fake(path,params):
            if path=='recording':return {'recordings':[{'id':RID,'title':'Colossal','artist-credit':CREDIT}]}
            if path=='recording/'+RID:return {'id':RID,'title':'Colossal','artist-credit':CREDIT,'releases':[{'id':RELEASE,'title':'Life Is Beautiful','status':'Official'}]}
            if path=='release/'+RELEASE:return {'id':RELEASE,'title':'Life Is Beautiful','release-group':{'id':GROUP},'media':[{'tracks':[{'title':'Colossal','recording':{'id':RID,'title':'Colossal','artist-credit':CREDIT}}]}]}
            raise AssertionError(path)
        with patch.object(edition_matching,'mb_get',side_effect=fake):
            result=await resolver.verified_recording_release_pairs('Larry June','Colossal','')
        self.assertEqual(result,[(RID,RELEASE,GROUP,'Life Is Beautiful')])

    async def test_multiple_recordings_are_not_guessed(self):
        async def fake(path,params):
            self.assertEqual(path,'recording')
            return {'recordings':[{'id':v,'title':'Colossal','artist-credit':CREDIT} for v in (RID,RID2)]}
        with patch.object(edition_matching,'mb_get',side_effect=fake):
            self.assertEqual(await resolver.verified_recording_release_pairs('Larry June','Colossal',''),[])

if __name__=='__main__':unittest.main()
