import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radar.service import Service

try:
    from radar import cloud_research
except ImportError:
    cloud_research = None


NOW = '2026-10-26T01:00:00Z'
URL = 'https://studio.itch.io/tower'
DESCRIPTION = 'Stack differently shaped blocks, balance the tower, and choose which unstable piece to remove before the next round.'
PAGE = '<html><head><title>Tower</title><meta property="og:description" content="' + DESCRIPTION + '"></head></html>'


def analysis(url=URL):
    return dict(scores=[None, None, 3, 3, 2, None],
                reasons=['缺少传播证据', '尚未观察实机', '官方介绍描述放置和移除方块，规则可拆成平衡原型',
                         '页面明确以堆叠及稳定为目标，适合先验证目标可读性', '形状与移除顺序可形成结构变体', '完整体验尚未核验'],
                evidence_urls=[url], mechanism='放置方块→平衡规则→失稳风险；需制作原型验证',
                risks='仅有官方描述，尚未检查实机或体验操作手感', next_step='寻找可回访的实机片段并试玩验证')


def response(payload, status='completed'):
    return {'id': 'resp_fixture', 'status': status, 'output': [
        {'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [
            {'type': 'output_text', 'text': json.dumps(payload, ensure_ascii=False), 'annotations': []}]}],
        'usage': {'input_tokens': 100, 'output_tokens': 200, 'total_tokens': 300}}


class CloudResearchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cloud_research, 'bounded cloud AI adapter is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Path(self.tmp.name), seeds=[])
        self.project = dict(id='itch:studio/tower', title='Tower', source='itch.io', url=URL,
                            stage='prototype', summary='', observed_at='2026-10-26T00:00:00Z',
                            event_at=None, event_time_status='unknown', event_type='directory_observation',
                            discovery='feed', evidence=[], metrics={})
        self.service.store.upsert_collection({'projects': [self.project], 'sources': []})
        self.config = {'OPENAI_API_KEY': 'unit-test-key-not-real', 'OPENAI_MODEL': 'fixture-model'}
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': '', 'OPENAI_MODEL': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_with(self, payload=None, page=PAGE):
        with patch.object(cloud_research.sources, '_fetch_text', return_value=page), \
                patch.object(cloud_research, '_request_response', return_value=response(payload or analysis())):
            return cloud_research.enrich(self.service, self.config, NOW)

    def test_missing_key_records_unconfigured_without_network_or_evaluation(self):
        with patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('network forbidden')), \
                patch.object(cloud_research, '_request_response', side_effect=AssertionError('paid call forbidden')):
            result = cloud_research.enrich(self.service, now=NOW)
        self.assertEqual(result['status'], 'unconfigured')
        self.assertEqual(result['evaluated'], 0)
        self.assertIsNone(self.service.store.projects()[0]['evaluation'])
        source = self.service.store.sources()[0]
        self.assertEqual((source['name'], source['status']), ('AI evidence analysis', 'unconfigured'))

    def test_valid_partial_analysis_saves_fetched_description_and_assistant_provenance(self):
        result = self.run_with()
        p = self.service.store.projects()[0]
        self.assertEqual(result['evaluated'], 1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(p['summary'], DESCRIPTION)
        self.assertEqual(p['evaluation']['scores'], [None, None, 3, 3, 2, None])
        self.assertEqual(p['evaluation']['author'], 'assistant')
        self.assertEqual(p['evaluation']['coverage'], 40)
        self.assertFalse(p['evaluation']['eligible'])
        self.assertEqual(p['evaluation']['evidence_urls'], [URL])
        self.assertIsNone(p['event_at'])
        self.assertEqual(p['evidence'][-1]['observed_at'], NOW)
        self.assertIn('未试玩', p['evaluation']['mechanism'])

    def test_metadata_cannot_promote_fun_spread_or_complete_experience(self):
        payload = analysis()
        payload['scores'] = [4] * 6
        self.run_with(payload)
        evaluation = self.service.store.projects()[0]['evaluation']
        self.assertEqual(evaluation['scores'], [None, None, 4, 4, 4, None])
        self.assertFalse(evaluation['eligible'])
        self.assertIn('未知', evaluation['reasons'][0])
        self.assertIn('未知', evaluation['reasons'][1])

    def test_unknown_remains_null_instead_of_zero(self):
        payload = analysis()
        payload['scores'] = [None] * 6
        self.run_with(payload)
        evaluation = self.service.store.projects()[0]['evaluation']
        self.assertEqual(evaluation['coverage'], 0)
        self.assertIsNone(evaluation['score'])

    def test_invented_url_invalid_score_and_invented_event_are_rejected(self):
        invalid = []
        item = analysis(); item['evidence_urls'] = ['https://example.com/fabricated']; invalid.append(item)
        item = analysis(); item['evidence_urls'] = [URL + '?invented=true']; invalid.append(item)
        item = analysis(); item['scores'][2] = True; invalid.append(item)
        item = analysis(); item['scores'][2] = 5; invalid.append(item)
        item = analysis(); item['scores'][2] = float('nan'); invalid.append(item)
        item = analysis(); item['reasons'][2] = ''; invalid.append(item)
        item = analysis(); item['reasons'][2] = '<script>alert(1)</script>'; invalid.append(item)
        item = analysis(); item['mechanism'] = 'https://example.com/secret'; invalid.append(item)
        item = analysis(); item['mechanism'] = '已试玩验证操作非常有趣'; invalid.append(item)
        item = analysis(); item['next_step'] = 'x' * 2001; invalid.append(item)
        item = analysis(); item['event_at'] = '2026-10-26'; invalid.append(item)
        for payload in invalid:
            with self.subTest(payload=payload):
                result = self.run_with(payload)
                self.assertEqual(result['evaluated'], 0)
                self.assertEqual(result['status'], 'failed')
                self.assertIsNone(self.service.store.projects()[0]['evaluation'])
                self.assertIsNone(self.service.store.projects()[0]['event_at'])

    def test_network_failure_never_exposes_exception_or_secret(self):
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', side_effect=OSError('unit-test-key-not-real private-detail')):
            result = cloud_research.enrich(self.service, self.config, NOW)
        exported = json.dumps(self.service.store.export()) + json.dumps(result)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('unit-test-key-not-real', exported)
        self.assertNotIn('private-detail', exported)
        self.assertIsNone(self.service.store.projects()[0]['evaluation'])

    def test_empty_or_blocked_page_never_spends_tokens(self):
        for page in ['<title>Tower</title>', '<title>Access Denied</title>' + PAGE]:
            with self.subTest(page=page), \
                    patch.object(cloud_research.sources, '_fetch_text', return_value=page), \
                    patch.object(cloud_research, '_request_response', side_effect=AssertionError('no usable evidence')):
                result = cloud_research.enrich(self.service, self.config, NOW)
                self.assertEqual(result['evaluated'], 0)

    def test_seed_stale_future_hidden_unsupported_and_user_evaluated_are_not_researched(self):
        alterations = [dict(discovery='seed'), dict(observed_at='2026-09-20T00:00:00Z'),
                       dict(observed_at='2026-10-27T00:00:00Z'), dict(url='https://example.com/game')]
        for changes in alterations:
            self.service.store.upsert_collection({'projects': [{**self.project, **changes}], 'sources': []})
            with patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('ineligible')):
                self.assertEqual(cloud_research.enrich(self.service, self.config, NOW)['evaluated'], 0)
        self.service.store.upsert_collection({'projects': [self.project], 'sources': []})
        self.service.store.evaluate({'project_id': self.project['id'], **analysis()}, author='user')
        with patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('user evaluation must survive')):
            self.assertEqual(cloud_research.enrich(self.service, self.config, NOW)['evaluated'], 0)
        self.assertEqual(self.service.store.projects()[0]['evaluation']['author'], 'user')

    def test_same_observation_is_not_charged_for_a_second_time(self):
        self.run_with()
        with patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('already evaluated')):
            result = cloud_research.enrich(self.service, self.config, NOW)
        self.assertEqual(result['evaluated'], 0)

    def test_hidden_or_fact_disputed_project_is_not_researched(self):
        for kind in ['hide', 'fact']:
            with self.subTest(kind=kind):
                feedback = self.service.store.add_feedback(dict(event_id=kind, project_id=self.project['id'],
                    type=kind, scope='project', note='需要核实项目身份'))
                with patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('excluded project')):
                    self.assertEqual(cloud_research.enrich(self.service, self.config, NOW)['evaluated'], 0)
                self.service.store.revoke(feedback['id'])

    def test_deadline_exhaustion_prevents_paid_call_and_preserves_unknown(self):
        with patch.object(cloud_research.time, 'monotonic', side_effect=[0, 151]), \
                patch.object(cloud_research.sources, '_fetch_text', side_effect=AssertionError('budget expired')), \
                patch.object(cloud_research, '_request_response', side_effect=AssertionError('budget expired')):
            result = cloud_research.enrich(self.service, self.config, NOW)
        self.assertEqual(result['evaluated'], 0)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(self.service.store.projects()[0]['evaluation'])

    def test_default_model_disables_reasoning_to_leave_output_budget_for_json(self):
        payloads = []
        def provider(payload, *args, **kwargs):
            payloads.append(payload)
            return response(analysis())
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', side_effect=provider):
            cloud_research.enrich(self.service, {'OPENAI_API_KEY': 'unit-test-key-not-real'}, NOW)
        self.assertEqual(payloads[0]['reasoning'], {'effort': 'none'})

    def test_malformed_deep_json_degrades_without_aborting_the_cloud_job(self):
        envelope = response(analysis())
        envelope['output'][0]['content'][0]['text'] = '[' * 1500 + ']' * 1500
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', return_value=envelope):
            result = cloud_research.enrich(self.service, self.config, NOW)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(self.service.store.projects()[0]['evaluation'])

    def test_decoder_recursion_failure_preserves_other_valid_candidate(self):
        extra = {**self.project, 'id': 'itch:studio/another', 'url': 'https://studio.itch.io/another'}
        self.service.store.upsert_collection({'projects': [extra], 'sources': []})
        responses = [RecursionError('provider JSON nesting'), response(analysis(extra['url']))]
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', side_effect=responses):
            result = cloud_research.enrich(self.service, self.config, NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['evaluated'], 1)
        p = next(p for p in self.service.store.projects() if p['id'] == extra['id'])
        self.assertEqual(p['evaluation']['evidence_urls'], [extra['url']])

    def test_candidate_cap_and_failure_do_not_discard_other_valid_evaluation(self):
        records = [{**self.project, 'id': 'itch:studio/game' + str(i),
                    'url': 'https://studio.itch.io/game' + str(i)} for i in range(20)]
        self.service.store.upsert_collection({'projects': records, 'sources': []})
        calls = []
        def provider(payload, api_key, timeout, deadline):
            data = json.loads(payload['input'][0]['content'])
            calls.append(data)
            if len(calls) == 1:
                raise ValueError('invalid provider result')
            return response(analysis(data['evidence'][0]['url']))
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', side_effect=provider):
            result = cloud_research.enrich(self.service, {**self.config, 'max_candidates': 1000}, NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['evaluated'], 9)
        self.assertEqual(len(calls), 10)

    def test_prompt_contains_description_only_and_has_no_model_tools(self):
        self.project['evidence'] = [{'url': URL, 'body': 'private user note', 'type': '用户提供'}]
        self.service.store.upsert_collection({'projects': [self.project], 'sources': []})
        requests = []
        def provider(payload, api_key, timeout, deadline):
            requests.append(payload)
            return response(analysis())
        with patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                patch.object(cloud_research, '_request_response', side_effect=provider):
            cloud_research.enrich(self.service, self.config, NOW)
        payload = requests[0]
        self.assertIn(DESCRIPTION, payload['input'][0]['content'])
        self.assertNotIn('private user note', json.dumps(payload))
        self.assertNotIn('unit-test-key-not-real', json.dumps(payload))
        self.assertNotIn('tools', payload)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['model'], 'fixture-model')
        self.assertLessEqual(payload['max_output_tokens'], 2000)
        self.assertTrue(payload['text']['format']['strict'])

    def test_incomplete_refusal_and_malformed_response_do_not_create_evaluation(self):
        cases = [response(analysis(), 'incomplete'), {'status': 'completed', 'output': []},
                 {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}]},
                 {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'not json'}]}]}]
        for envelope in cases:
            with self.subTest(envelope=envelope), \
                    patch.object(cloud_research.sources, '_fetch_text', return_value=PAGE), \
                    patch.object(cloud_research, '_request_response', return_value=envelope):
                result = cloud_research.enrich(self.service, self.config, NOW)
                self.assertEqual(result['evaluated'], 0)
                self.assertIsNone(self.service.store.projects()[0]['evaluation'])

    def test_steam_detail_refresh_preserves_discovery_and_event_time(self):
        self.service.store.upsert_collection({'projects': [{**self.project, 'id': 'steam:1',
            'url': 'https://store.steampowered.com/app/1/', 'source': 'Steam'}], 'sources': []})
        def fetch(url, **kwargs):
            if '/api/appdetails?' in url:
                return json.dumps({'1': {'success': True, 'data': {'name': 'Tower', 'type': 'game',
                    'short_description': DESCRIPTION, 'release_date': {'coming_soon': False, 'date': '24 Oct, 2026'}}}})
            return PAGE
        def provider(payload, *args, **kwargs):
            data = json.loads(payload['input'][0]['content'])
            return response(analysis(data['evidence'][0]['url']))
        with patch.object(cloud_research.sources, '_fetch_text', side_effect=fetch), \
                patch.object(cloud_research, '_request_response', side_effect=provider):
            result = cloud_research.enrich(self.service, self.config, NOW)
        p = next(p for p in self.service.store.projects() if p['id'] == 'steam:1')
        self.assertEqual(result['evaluated'], 2)
        self.assertEqual(p['discovery'], 'feed')
        self.assertIsNone(p['event_at'])
        self.assertEqual(p['summary'], DESCRIPTION)

    def test_transport_uses_fixed_host_no_redirects_and_bounded_body(self):
        class Connection:
            sock = None
            status = 200
            body = io.BytesIO(json.dumps(response(analysis())).encode())
            def __init__(self, host, **kwargs):
                self.host, self.kwargs = host, kwargs
                captured.append(self)
            def request(self, method, target, body, headers):
                self.sent = (method, target, body, headers)
            def getresponse(self): return self
            def read1(self, size): return self.body.read(size)
            def close(self): self.closed = True
        captured = []
        with patch.object(cloud_research.http.client, 'HTTPSConnection', Connection):
            envelope = cloud_research._request_response({'model': 'fixture'}, 'not-a-real-key', 3, cloud_research.time.monotonic()+5)
        self.assertEqual(envelope['status'], 'completed')
        self.assertEqual(captured[0].host, 'api.openai.com')
        self.assertEqual(captured[0].sent[:2], ('POST', '/v1/responses'))
        self.assertTrue(captured[0].closed)
        Connection.status = 302
        with patch.object(cloud_research.http.client, 'HTTPSConnection', Connection), self.assertRaises(OSError):
            cloud_research._request_response({}, 'not-a-real-key', 3, cloud_research.time.monotonic()+5)
        self.assertEqual(len(captured), 2)
        Connection.status = 200
        Connection.body = io.BytesIO(b'x' * 300000)
        with patch.object(cloud_research.http.client, 'HTTPSConnection', Connection), self.assertRaises(ValueError):
            cloud_research._request_response({}, 'not-a-real-key', 3, cloud_research.time.monotonic()+5)


if __name__ == '__main__':
    unittest.main()
