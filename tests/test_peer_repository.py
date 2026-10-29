"""Run inside the custom DroppedNeedle image with /app on PYTHONPATH."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,'/app')
from repositories.slskd import peer_scheduler as s
from repositories.slskd.slskd_repository import SlskdRepository
from repositories.slskd.slskd_models import SlskdEnqueueResponse,SlskdTransfer
from repositories.protocols.download_client import EnqueueRequest,DownloadFileRef


class FakeClient:
    def __init__(self):self.calls=[]
    async def enqueue(self,peer,files):
        self.calls.append((peer,files))
        return SlskdEnqueueResponse(enqueued=[{'filename':f['filename']} for f in files])
    async def get_downloads(self,peer):return getattr(self,'transfers',[])


class RepositorySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=s.DB
        s.DB=os.path.join(self.temp.name,'scheduler.sqlite')
        os.environ['PEER_SCHEDULER_ENABLED']='true'
        self.client=FakeClient()
        self.repo=SlskdRepository(self.client,'http://slskd','key',Path('/tmp'))

    def tearDown(self):
        s.DB=self.old
        os.environ.pop('PEER_SCHEDULER_ENABLED',None)
        self.temp.cleanup()

    def test_enqueue_uses_peer_admission_and_records_job(self):
        req=EnqueueRequest(task_id='task-1',source='soulseek',files=[
            DownloadFileRef(username='Peer',filename='a.flac',size=1000)])
        handle=asyncio.run(self.repo.enqueue(req))
        self.assertEqual(handle.username,'Peer')
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(s.snapshot()['batches'][0]['state'],'queued')

    def test_explicit_block_prevents_enqueue(self):
        s.set_peer('Peer',blocked=True)
        req=EnqueueRequest(task_id='task-2',source='soulseek',files=[
            DownloadFileRef(username='Peer',filename='b.flac',size=1000)])
        with self.assertRaisesRegex(RuntimeError,'blocked'):
            asyncio.run(self.repo.enqueue(req))
        self.assertEqual(self.client.calls,[])

    def test_reconciles_existing_slskd_transfer_without_enqueue(self):
        s.reserve('task-3:source','Peer',[{'filename':'a.flac','size':1000}])
        self.client.transfers=[SlskdTransfer(id='transfer-1',username='Peer',
            filename='a.flac',size=1000,bytes_transferred=1000,
            state='Completed, Succeeded')]
        asyncio.run(self.repo._scheduler_reconcile_once())
        self.assertEqual(s.snapshot()['batches'][0]['state'],'completed')
        self.assertEqual(self.client.calls,[])


if __name__=='__main__':unittest.main()
