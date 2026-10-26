import tempfile
import unittest
from pathlib import Path

try:
    from radar.store import Store, score_evaluation
except ImportError:
    Store = score_evaluation = None


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Store, 'persistent Store has not been implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'radar.sqlite3')
        self.project = dict(id='steam:1', title='Example Demo', source='Steam',
                            url='https://store.steampowered.com/app/1/', stage='demo',
                            tags=['合作'], summary='demo', observed_at='2026-10-25T00:00:00Z',
                            event_at='2026-10-24T00:00:00Z', event_type='demo',
                            event_time_status='known', discovery='feed', evidence=[], metrics={})
        self.store.upsert_collection({'projects': [self.project], 'sources': []})

    def feedback(self, **kwargs):
        data = dict(event_id='event1', project_id='steam:1', type='seen', scope='project', note='')
        data.update(kwargs)
        return self.store.add_feedback(data)

    def evaluation(self):
        return self.store.evaluate(dict(project_id='steam:1', scores=[4]*6,
             reasons=['实机中观察到具体选择和反馈']*6,
             evidence_urls=[self.project['url']], mechanism='输入→规则→反馈',
             risks='尚未试玩', next_step='试玩验证'))

    def test_feedback_idempotency_and_revoke_preserve_history(self):
        one = self.feedback()
        two = self.feedback()
        self.assertEqual(one['id'], two['id'])
        self.assertTrue(self.store.projects()[0]['flags']['seen'])
        self.store.revoke(one['id'])
        self.assertFalse(self.store.projects()[0]['flags']['seen'])
        self.assertTrue(self.store.feedback()[0]['revoked'])
        self.assertEqual(len(Store(self.store.path).feedback()), 1)

    def test_event_id_conflict_is_not_silently_accepted(self):
        self.feedback()
        with self.assertRaises(ValueError):
            self.feedback(type='hide')

    def test_project_negative_never_becomes_global_rule(self):
        self.feedback(type='boring', note='我玩起来无趣')
        self.assertEqual(self.store.rules()['rules'], [])
        self.assertEqual(self.store.projects()[0]['preference_adjustment'], 0)

    def test_fact_correction_cannot_be_preference(self):
        with self.assertRaises(ValueError):
            self.feedback(type='fact', scope='preference', note='名称不对')

    def test_explicit_rule_changes_selection_and_revoke_restores(self):
        self.evaluation()
        first = self.store.create_report('2026-10-25T01:00:00Z')
        self.assertEqual(len(first['recommendations']), 1)
        f = self.feedback(type='other', scope='preference', note='以后排除合作玩法',
                          rule={'action':'avoid', 'tag':'合作'})
        self.assertTrue(self.store.projects()[0]['flags']['excluded_by_rule'])
        self.assertEqual(self.store.create_report('2026-11-02T01:00:00Z')['recommendations'], [])
        version = self.store.rules()['version']
        self.store.revoke(f['id'])
        self.assertNotEqual(version, self.store.rules()['version'])
        self.project['observed_at']='2026-11-03T00:00:00Z'
        self.store.upsert_collection({'projects':[self.project], 'sources':[]})
        self.assertEqual(len(self.store.create_report('2026-11-03T01:00:00Z')['recommendations']), 1)
        self.assertEqual(len(self.store.report(first['id'])['recommendations']), 1)

    def test_coverage_unknown_is_not_zero_and_core_needed(self):
        self.assertEqual(score_evaluation([None]*6)['score'], None)
        partial = score_evaluation([4, None, 4,4,4,4])
        self.assertEqual(partial['score'], 100)
        self.assertFalse(partial['eligible'])

    def test_score_without_evidence_rejected(self):
        with self.assertRaises(ValueError):
            self.store.evaluate(dict(project_id='steam:1',scores=[4]*6,reasons=['']*6,evidence_urls=[]))

    def test_unknown_event_time_not_made_new_by_collection(self):
        self.project['event_at'] = None
        self.store.upsert_collection({'projects':[self.project], 'sources':[]})
        self.evaluation()
        report = self.store.create_report('2026-10-25T01:00:00Z')
        self.assertEqual(report['recommendations'], [])
        self.assertEqual(len(report['observations']), 1)

    def test_failed_refresh_preserves_previous_projects(self):
        self.store.upsert_collection({'projects':[], 'sources':[dict(name='Steam',status='failed',count=0,message='timeout')]})
        self.assertEqual(len(self.store.projects()), 1)
        self.assertEqual(self.store.sources()[0]['status'], 'failed')

    def test_partial_project_refresh_preserves_evidence_with_metric_age(self):
        self.project.update(evidence=[{'type':'用户提供','url':self.project['url'],'body':'valuable note'}],metrics={'review_count':50})
        self.store.upsert_collection({'projects':[self.project], 'sources':[]})
        update={**self.project,'evidence':[],'metrics':{},'observed_at':'2026-10-26T00:00:00Z'}
        self.store.upsert_collection({'projects':[update], 'sources':[]})
        p=self.store.projects()[0]
        self.assertEqual(p['evidence'][0]['body'],'valuable note')
        self.assertEqual(p['metrics']['review_count'],50)
        self.assertEqual(p['metric_observed_at']['review_count'],'2026-10-25T00:00:00Z')

    def test_uncertain_quick_action_retry_does_not_duplicate_active_follow(self):
        a=self.feedback(type='follow')
        b=self.feedback(type='follow',event_id='event2')
        self.assertEqual(a['id'],b['id'])
        self.store.revoke(a['id'])
        self.assertFalse(self.store.projects()[0]['flags']['followed'])

    def test_failed_review_refresh_retains_successful_sample_and_failure_time(self):
        sample={'status':'ok','observed_at':'2026-10-25T00:00:00Z','sample_n':1,
                'reviews':[{'text':'内容不足，但机制好玩','tentative_labels':['内容量']}]}
        self.store.upsert_collection({'projects':[{**self.project,'review_sample':sample}]})
        failure={'status':'failed','observed_at':'2026-10-26T00:00:00Z','reviews':[],
                 'sample_n':None,'error':'TimeoutError'}
        self.store.upsert_collection({'projects':[{**self.project,'review_sample':failure}]})
        p=self.store.projects()[0]
        self.assertEqual(p['review_sample'],sample)
        self.assertEqual(p['review_sample_last_attempt'],failure)
        recovered={**sample,'observed_at':'2026-10-27T00:00:00Z','sample_n':0,'reviews':[]}
        self.store.upsert_collection({'projects':[{**self.project,'review_sample':recovered}]})
        self.assertEqual(self.store.projects()[0]['review_sample'],recovered)
        self.assertEqual(self.store.projects()[0]['review_sample_last_attempt']['status'],'ok')

    def test_directory_refresh_cannot_erase_verified_event_or_summary(self):
        self.store.upsert_collection({'projects':[{**self.project,'summary':'已查证的玩法摘要'}]})
        update={**self.project,'event_at':None,'event_time_status':'unknown',
                'event_type':'directory_observation','stage':'unknown','summary':'',
                'observed_at':'2026-10-26T00:00:00Z'}
        self.store.upsert_collection({'projects':[update]})
        p=self.store.projects()[0]
        self.assertEqual(p['event_at'],self.project['event_at'])
        self.assertEqual(p['event_type'],'demo')
        self.assertEqual(p['event_provenance']['observed_at'],self.project['observed_at'])
        self.assertEqual(p['observed_at'],update['observed_at'])
        self.assertEqual(p['summary'],'已查证的玩法摘要')
        self.assertEqual(p['stage'],'demo')

    def test_old_observation_or_current_source_failure_blocks_fresh_recommendation(self):
        self.evaluation()
        self.assertEqual(self.store.create_report('2026-10-27T01:00:00Z')['recommendations'],[])
        self.store.upsert_collection({'projects':[], 'sources':[dict(name='Steam',status='failed',count=0,message='timeout')]})
        self.assertEqual(self.store.create_report('2026-10-25T01:00:00Z')['recommendations'],[])

    def test_same_signal_not_recommended_twice_in_seven_days(self):
        self.evaluation()
        self.assertEqual(len(self.store.create_report('2026-10-25T01:00:00Z')['recommendations']),1)
        self.assertEqual(len(self.store.create_report('2026-10-26T01:00:00Z')['recommendations']),0)

    def test_collection_snapshot_does_not_consume_final_recommendation(self):
        self.evaluation()
        draft=self.store.create_report('2026-10-25T01:00:00Z',kind='collection')
        self.assertEqual(draft['kind'],'collection')
        final=self.store.create_report('2026-10-25T02:00:00Z')
        self.assertEqual(len(final['recommendations']),1)
        self.assertEqual(self.store.create_report('2026-10-25T03:00:00Z')['recommendations'],[])

    def test_reference_seed_not_a_new_opportunity(self):
        self.project['discovery']='seed'
        self.store.upsert_collection({'projects':[self.project], 'sources':[]})
        self.evaluation()
        self.assertEqual(self.store.create_report('2026-10-25T01:00:00Z')['recommendations'], [])

    def test_demo_and_verified_parent_do_not_take_two_recommendation_slots(self):
        self.evaluation()
        child={**self.project,'id':'steam:2','demo_parent_id':'steam:1'}
        self.store.upsert_collection({'projects':[child],'sources':[]})
        self.store.evaluate(dict(project_id='steam:2',scores=[4]*6,reasons=['具体证据']*6,evidence_urls=[child['url']]))
        report=self.store.create_report('2026-10-25T01:00:00Z')
        self.assertEqual(len(report['recommendations']),1)

    def test_delivery_claim_prevents_concurrent_or_unknown_retries(self):
        report=self.store.create_report('2026-10-25T01:00:00Z')
        self.assertTrue(self.store.claim_delivery(report['id'],'target'))
        self.assertFalse(self.store.claim_delivery(report['id'],'target'))
        self.store.finish_delivery(report['id'],'target',{'status':'unknown','error':'timeout'})
        self.assertFalse(self.store.claim_delivery(report['id'],'target'))


if __name__ == '__main__':
    unittest.main()
