import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

try:
    from radar.service import Service, render_report
    from radar.server import make_server
except ImportError:
    Service = render_report = make_server = None


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Service, 'local service has not been implemented')
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service=Service(Path(self.tmp.name), root=Path(__file__).resolve().parents[1], seeds=[],
                             collector=lambda *args,**kw: {'projects':[], 'sources':[dict(name='Steam',status='failed',count=0,message='test failure')]})
        self.http=make_server(self.service,port=0)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base='http://127.0.0.1:'+str(self.http.server_port)
        self.csrf=self.request('/api/state')['csrf']

    def request(self,path,body=None,headers=None):
        headers=headers or {}
        if body is not None:
            headers={'Content-Type':'application/json','X-Radar-CSRF':self.csrf,**headers}
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                raw=response.read().decode()
                return json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
        except urllib.error.HTTPError as error:
            error.close()
            raise

    def test_workbench_loads_and_does_not_serve_private_files(self):
        self.assertIn('html',self.request('/'))
        for path in ['/data/radar.sqlite3','/config.json','/../PRD.md','/%2e%2e/PRD.md']:
            with self.assertRaises(urllib.error.HTTPError) as error: self.request(path)
            self.assertEqual(error.exception.code,404)

    def test_restart_shows_last_completed_collection_without_restarting_it(self):
        previous=dict(id='saved-job',status='completed',message='已采集15条候选',finished_at='2026-10-25T00:00:00Z')
        self.service.store.set_settings({'last_job':previous},internal=True)
        restarted=Service(self.service.data_dir,seeds=[])
        self.assertEqual(restarted.job,previous)
        self.assertFalse(restarted.job_lock.locked())

    def test_post_requires_csrf_and_same_origin(self):
        for headers in [{'X-Radar-CSRF':'bad'},{'Origin':'https://evil.invalid'}]:
            with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/report',{},headers)
            self.assertEqual(error.exception.code,403)

    def test_host_header_not_rebindable(self):
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/state',headers={'Host':'evil.invalid'})
        self.assertEqual(error.exception.code,403)

    def test_manual_url_does_not_fetch_or_trust_note(self):
        result=self.request('/api/project',{'url':'https://store.steampowered.com/app/5245810/','title':'ForgeTax','note':'肯定是爆款'})
        p=result['project']
        self.assertEqual(p['discovery'],'manual')
        self.assertIsNone(p['event_at'])
        self.assertEqual(p['evidence'][0]['type'],'用户提供，待核实')

    def test_private_and_unsupported_urls_rejected(self):
        for url in ['https://127.0.0.1/a','file:///secret','https://evil.invalid/a','https://store.steampowered.com@127.0.0.1/a']:
            with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/project',{'url':url})
            self.assertEqual(error.exception.code,400)

    def test_failed_sources_produce_honest_saved_report(self):
        self.request('/api/collect',{})
        for _ in range(100):
            job=self.request('/api/job')
            if job['status']!='running': break
            time.sleep(.01)
        self.assertEqual(job['status'],'completed')
        state=self.request('/api/state')
        self.assertEqual(state['report']['recommendations'],[])
        self.assertEqual(state['sources'][0]['status'],'failed')
        self.assertIn('无新增',state['report']['summary'])
        self.assertIn('test failure',self.request('/reports/'+state['report']['id']+'.html'))

    def test_no_credentials_no_send_or_delivery_claim(self):
        self.service.feishu_config={'mode':'webhook'}
        r=self.request('/api/report',{})['report']
        result=self.request('/api/send',{'report_id':r['id']})
        self.assertEqual(result['status'],'unconfigured')
        self.assertEqual(self.service.store.deliveries(),[])

    def test_send_requires_explicit_report_identity(self):
        self.request('/api/report',{})
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/send',{})
        self.assertEqual(error.exception.code,400)

    def test_research_collection_can_suppress_draft_delivery(self):
        sent=[]
        self.service.configured=lambda: True
        self.service.send=lambda report_id: sent.append(report_id)
        self.service.store.set_settings({'auto_send':True})
        self.service.job_lock.acquire()
        self.service._collect(send_after_collection=False)
        self.assertEqual(self.service.job['status'],'completed')
        self.assertEqual(sent,[])
        draft=self.service.store.report()
        self.assertEqual(draft['kind'],'collection')
        # Invoke the real method: a collection snapshot must not be sent as final.
        self.assertEqual(Service.send(self.service,draft['id'])['status'],'draft')
        self.service.job_lock.acquire()
        self.service._collect()
        self.assertEqual(sent,[self.service.job['report_id']])

    def test_daily_schedule_uses_shanghai_and_runs_at_most_once(self):
        from datetime import datetime,timezone
        calls=[]
        self.service.start_collection=lambda: calls.append('run')
        self.service.store.set_settings({'schedule_enabled':True,'daily_time':'09:00'})
        self.service.scheduler_tick(datetime(2026,10,25,0,59,tzinfo=timezone.utc))
        self.assertEqual(calls,[])
        self.service.scheduler_tick(datetime(2026,10,25,1,0,tzinfo=timezone.utc))
        self.service.scheduler_tick(datetime(2026,10,25,2,0,tzinfo=timezone.utc))
        self.assertEqual(calls,['run'])

    def test_generated_report_escapes_source_html(self):
        self.assertNotIn('<script>alert(1)</script>',render_report(dict(id='x',date='x',created_at='x',rule_version='x',summary='<script>alert(1)</script>',recommendations=[],observations=[],sources=[])))

    def test_report_contains_evaluation_evidence_not_just_product_link(self):
        p=dict(title='Demo',url='https://example.com/game',evidence=[dict(type='实机',title='Clip',body='观察',url='https://example.com/clip',observed_at='2026-10-24T00:00:00Z')],evaluation=dict(scores=[4]*6,reasons=['specific reason']*6,evidence_urls=['https://example.com/research'],next_step='test controls'))
        rendered=render_report(dict(id='x',date='x',created_at='x',rule_version='x',summary='',recommendations=[p],observations=[],sources=[]))
        for item in ['https://example.com/clip','https://example.com/research','specific reason','test controls','2026-10-24T00:00:00Z']:
            self.assertIn(item,rendered)


if __name__=='__main__': unittest.main()
