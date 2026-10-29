"""Run with: python3 -m unittest -v tests.test_peer_scheduler"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'droppedneedle-recovery-v1'))
import peer_scheduler as s


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=s.DB
        s.DB=os.path.join(self.tmp.name,'scheduler.sqlite')
        s.initialize()

    def tearDown(self):
        s.DB=self.old
        self.tmp.cleanup()

    def test_defaults_and_persistence(self):
        self.assertEqual(s.settings()['active_batches_per_peer'],1)
        s.update_settings({'active_batches_per_peer':2,'paused':True})
        self.assertEqual(s.settings()['active_batches_per_peer'],2)
        self.assertEqual(s.eligibility('Peer'), 'Scheduler paused')
        s.update_settings({'paused':False})

    def test_atomic_peer_limit_and_history(self):
        files=[{'filename':'a.flac','size':1000}]
        self.assertEqual(s.reserve('job1','Peer',files),(True,''))
        admitted,why=s.reserve('job2','Peer',files)
        self.assertFalse(admitted)
        self.assertIn('Peer queue',why)
        self.assertEqual({b['task_id']:b['state'] for b in s.snapshot()['batches']}['job2'],'deferred')
        s.mark('job1','completed','verified',progress=100,bytes_moved=1000,files_done=1)
        self.assertEqual(s.snapshot()['peers'][0]['files_downloaded'],1)
        self.assertTrue(any(e['kind']=='completed' for e in s.peer_history('Peer')))

    def test_block_and_override(self):
        s.set_peer('Peer',blocked=True,rule_text='No automated download')
        self.assertEqual(s.eligibility('Peer'),'Peer explicitly blocked')
        s.set_peer('Peer',blocked=False,limits={'queued_files_per_peer':1})
        self.assertEqual(s.eligibility('Peer',2,100),'Batch exceeds peer queue limit')

    def test_search_cache_and_rate_gate(self):
        s.store_search('artist title',[{'filename':'demo.flac'}])
        self.assertEqual(s.cached_search('artist title')[0]['filename'],'demo.flac')
        self.assertEqual(s.reserve_search(),0)
        self.assertGreater(s.reserve_search(),0)

    def test_retry_controls_and_stall_threshold(self):
        files=[{'filename':'a.flac','size':1000}]
        s.update_settings({'retry_backoff_seconds':0,'batch_cooldown_seconds':0,
                           'stalled_seconds':1})
        self.assertTrue(s.reserve('dn-task:first','Peer',files)[0])
        s.mark('dn-task:first','downloading','moving',bytes_moved=100)
        self.assertFalse(s.is_stalled('dn-task:first',100))
        with s.connect() as db:
            db.execute('UPDATE batches SET progress_at=progress_at-10 WHERE task_id=?',('dn-task:first',))
        self.assertTrue(s.is_stalled('dn-task:first',100))
        s.mark('dn-task:first','failed','stalled')
        s.update_settings({'auto_retries':False})
        self.assertEqual(s.reserve('dn-task:second','Other',files)[1],
                         'Automatic retries disabled')

    def test_reliability_only_breaks_equivalent_candidate_ties(self):
        s.set_peer('Good')
        s.set_peer('Bad')
        with s.connect() as db:
            db.execute('UPDATE peers SET success_count=9 WHERE name=?',('Good',))
            db.execute('UPDATE peers SET failure_count=9 WHERE name=?',('Bad',))
        def candidate(peer,score):
            return SimpleNamespace(username=peer,tier='auto',final_score=score,
                quality_decision=None,files=[SimpleNamespace(extension='flac')])
        bad=candidate('Bad',0.91);good=candidate('Good',0.91)
        self.assertEqual(s.rerank([bad,good]),[good,bad])
        distinct=candidate('Bad',0.97)
        self.assertEqual(s.rerank([distinct,good]),[distinct,good])

    def test_acquisition_phase_preserves_verified_alternatives(self):
        s.note_phase('task','searching',reason='Search started')
        s.note_phase('task','candidate_ready',selected_peer='Peer',
                     alternatives=['Other'],reason='Two verified peers')
        s.note_phase('task','verifying',selected_peer='Peer',reason='Import check')
        phase=s.snapshot()['phases'][0]
        self.assertEqual(phase['state'],'verifying')
        self.assertEqual(phase['selected_peer'],'Peer')
        self.assertEqual(phase['alternatives'],['Other'])


if __name__=='__main__': unittest.main()
