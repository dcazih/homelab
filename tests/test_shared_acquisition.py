import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'profiles'))
import shared_acquisition as shared


class SharedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'bridge.db'
        shared.initialize(self.connection)

    def tearDown(self):self.temp.cleanup()

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path)
        db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        finally:db.close()

    def test_only_one_profile_submits_exact_recording_edition(self):
        mbid='11111111-1111-1111-1111-111111111111'
        release='22222222-2222-2222-2222-222222222222'
        owner,_=shared.claim(self.connection,mbid,release,'julia-proposal')
        follower,row=shared.claim(self.connection,mbid,release,'spammz-proposal')
        self.assertTrue(owner)
        self.assertFalse(follower)
        self.assertEqual(row['state'],'submitting')
        shared.finish(self.connection,mbid,release,'julia-proposal','accepted','dn-task')
        follower,row=shared.claim(self.connection,mbid,release,'spammz-proposal')
        self.assertFalse(follower)
        self.assertEqual(row['dn_task_id'],'dn-task')
        other_release='33333333-3333-3333-3333-333333333333'
        self.assertTrue(shared.claim(self.connection,mbid,other_release,'other-edition')[0])


if __name__=='__main__':unittest.main()
