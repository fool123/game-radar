import json
from pathlib import Path
import tempfile
import unittest
from radar.service import Service


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.service=Service(self.path/'runtime',seeds=[])
        self.service.store.upsert_collection({'projects':[dict(id='steam:1',title='Public game',url='https://store.steampowered.com/app/1/',source='Steam',discovery='feed',observed_at='2026-10-26T00:00:00Z',summary='Public description',evidence=[],metrics={})]})

    def issue(self, **updates):
        payload=dict(schema='game-radar-feedback/v1',project_id='steam:1',type='other',scope='preference',note='more puzzles',rule=dict(action='prefer',tag='puzzle'))
        result=dict(number=1,user={'login':'owner'},state='open',updated_at='2026-10-26T01:00:00Z',body='```json\n'+json.dumps(payload)+'\n```')
        result.update(updates);return result

    def test_only_authorized_issues_apply_and_closure_revokes(self):
        from radar.cloud import sync_feedback
        sync_feedback(self.service,[self.issue(user={'login':'stranger'})],'owner/game-radar')
        self.assertEqual(self.service.store.rules()['rules'],[])
        sync_feedback(self.service,[self.issue()],'owner/game-radar')
        self.assertEqual(len(self.service.store.rules()['rules']),1)
        sync_feedback(self.service,[self.issue()],'owner/game-radar')
        self.assertEqual(len(self.service.store.feedback()),1)
        sync_feedback(self.service,[self.issue(state='closed')],'owner/game-radar')
        self.assertEqual(self.service.store.rules()['rules'],[])

    def test_reopened_issue_can_reapply_after_revocation(self):
        from radar.cloud import sync_feedback
        sync_feedback(self.service,[self.issue()],'owner/game-radar')
        sync_feedback(self.service,[self.issue(state='closed')],'owner/game-radar')
        sync_feedback(self.service,[self.issue(updated_at='2026-10-26T03:00:00Z')],'owner/game-radar')
        self.assertEqual(len(self.service.store.rules()['rules']),1)

    def test_same_day_report_reused(self):
        from radar.cloud import daily_report
        a=daily_report(self.service,'2026-10-26T01:00:00Z')
        b=daily_report(self.service,'2026-10-26T04:00:00Z')
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(len(self.service.store.reports()),1)

    def test_public_export_uses_whitelist(self):
        from radar.cloud import public_state
        self.service.store.upsert_collection({'projects':[dict(id='steam:1',private_note='PRIVATE',evidence=[dict(type='用户提供，待核实',body='PRIVATE',url='https://example.com')])]})
        state=public_state(self.service,'owner/game-radar')
        raw=json.dumps(state)
        self.assertNotIn('PRIVATE',raw)
        for key in ['csrf','settings','feedback','deliveries']:self.assertNotIn(key,state)

    def test_later_collection_does_not_replace_final_report(self):
        from radar.cloud import daily_report,public_state
        final=daily_report(self.service,'2026-10-26T01:00:00Z')
        self.service.store.create_report('2026-10-26T02:00:00Z',kind='collection')
        self.assertEqual(public_state(self.service,'owner/game-radar')['report']['id'],final['id'])

    def test_backup_restores_persistent_records(self):
        from radar.cloud import backup,restore
        backup(self.service,self.path/'state.sqlite3')
        restore(self.path/'state.sqlite3',self.path/'restored')
        reopened=Service(self.path/'restored',seeds=[])
        self.assertEqual(reopened.store.projects()[0]['id'],'steam:1')

    def test_send_requires_durable_claim_and_reuses_id(self):
        from radar.cloud import daily_report, claim_send, deliver_claim
        self.service.feishu_config=dict(mode='webhook',webhook_url='https://open.feishu.cn/open-apis/bot/v2/hook/test')
        r=daily_report(self.service,'2026-10-26T01:00:00Z')
        ticket=claim_send(self.service,r['id'],True)
        self.assertIsNotNone(ticket)
        self.assertIsNone(claim_send(self.service,r['id'],True))
        calls=[]
        def sender(*args):calls.append(1);return {'status':'sent'}
        with self.assertRaises(ValueError):deliver_claim(self.service,ticket,False,sender)
        self.assertEqual(calls,[])
        deliver_claim(self.service,ticket,True,sender)
        deliver_claim(self.service,ticket,True,sender)
        self.assertEqual(calls,[1])


if __name__=='__main__':unittest.main()
