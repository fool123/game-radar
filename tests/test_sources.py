import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from radar import sources


NOW = '2026-10-24T00:00:00Z'
RSS = '''<rss><channel><item><title>Sample prototype</title>
<link>https://dev.itch.io/sample?source=rss</link>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>A small puzzle</p>]]></description>
</item></channel></rss>'''
SEARCH = '''<a class="search_result_row" data-ds-appid="123" href="https://store.steampowered.com/app/123/Thing_Demo/?snr=x">
<span class="title">Thing Demo</span><div class="search_released">23 Oct, 2026</div></a>'''


class SourceTests(unittest.TestCase):
    def test_identity_keeps_demo_separate_and_ignores_tracking(self):
        self.assertEqual(sources.normalize_url('https://store.steampowered.com/app/123/Thing_Demo/?snr=x#hi'),
                         'https://store.steampowered.com/app/123/')
        self.assertEqual(sources.project_id('https://store.steampowered.com/app/123/Name'), 'steam:123')
        self.assertNotEqual(sources.project_id('https://store.steampowered.com/app/123/'),
                            sources.project_id('https://store.steampowered.com/app/124/'))
        self.assertEqual(sources.project_id('https://dev.itch.io/sample?x=1'),
                         sources.project_id('https://dev.itch.io/sample/'))

    def test_rejects_unsafe_urls_and_non_platform_collection(self):
        for url in ['http://127.0.0.1/', 'https://127.0.0.1/', 'https://[::1]/',
                    'https://user:secret@itch.io/', 'https://itch.io:8443/',
                    'file:///etc/passwd', 'https://localhost/', 'https://10.1.2.3/',
                    'https://itch.io\\@127.0.0.1/', 'https://itch.io/\r\nX: y']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources.normalize_url(url)
        with self.assertRaises(ValueError):
            sources._network_target('https://itch.io.attacker.example/')

    def test_dns_private_or_mixed_answer_fails_closed(self):
        answers = [(2, 1, 6, '', ('93.184.216.34', 443)),
                   (2, 1, 6, '', ('127.0.0.1', 443))]
        with patch('radar.sources.socket.getaddrinfo', return_value=answers):
            with self.assertRaises(ValueError):
                sources._network_target('https://itch.io/feed/new.xml')

    def test_rss_publication_is_not_release_or_collection_date(self):
        rows = sources.parse_itch_rss(RSS, 'https://itch.io/feed/new.xml', NOW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['observed_at'], NOW)
        self.assertEqual(row['event_at'], '2026-10-23T10:00:00Z')
        self.assertEqual(row['event_type'], 'feed_publication')
        self.assertEqual(row['stage'], 'unknown')
        self.assertEqual(row['metrics'], {})
        no_date = sources.parse_itch_rss(RSS.replace('Fri, 23 Oct 2026 10:00:00 GMT', 'bad'),
                                         'https://itch.io/feed/new.xml', NOW)[0]
        self.assertIsNone(no_date['event_at'])
        self.assertEqual(no_date['event_time_status'], 'unknown')

    def test_search_demo_identity_does_not_assert_release_date(self):
        rows = sources.parse_steam_search(SEARCH, 'https://store.steampowered.com/search/', NOW, 'demo')
        self.assertEqual(rows[0]['id'], 'steam:123')
        self.assertEqual(rows[0]['stage'], 'demo')
        self.assertIsNone(rows[0]['event_at'])

    def test_malformed_and_untrusted_feed_items_do_not_become_candidates(self):
        with self.assertRaises(ValueError):
            sources.parse_itch_rss('<html>rate limited</html>', 'https://itch.io/feed/new.xml', NOW)
        malicious = RSS.replace('https://dev.itch.io/sample?source=rss', 'https://127.0.0.1/secret')
        self.assertEqual(sources.parse_itch_rss(malicious, 'https://itch.io/feed/new.xml', NOW), [])

    def test_failure_is_explicit_and_other_source_keeps_success(self):
        def fetch(url, **kwargs):
            if url == 'https://itch.io/feed/new.xml':
                return RSS
            raise OSError('network unreachable')
        with patch('radar.sources._fetch_text', side_effect=fetch):
            result = sources.collect({'max_requests': 8}, [], NOW)
        self.assertEqual(len(result['projects']), 1)
        by_source = {s['name']: s for s in result['sources']}
        self.assertEqual(by_source['itch.io RSS']['status'], 'ok')
        self.assertEqual(by_source['Steam 公开发现']['status'], 'failed')
        self.assertEqual(by_source['TapTap Maker 目录']['status'], 'failed')

    def test_reference_seed_not_promoted_to_feed_and_failed_seed_not_fabricated(self):
        payload = json.dumps({'123': {'success': True, 'data': {
            'name': 'Thing Demo', 'type': 'demo', 'fullgame': {'appid': '124'},
            'short_description': 'A puzzle', 'release_date': {'coming_soon': False, 'date': '23 Oct, 2026'}
        }}})
        def fetch(url, **kwargs):
            if '/api/appdetails?' in url:
                return payload
            if '/search/' in url:
                return SEARCH
            raise OSError('unreachable')
        seeds = [{'url': 'https://store.steampowered.com/app/123/', 'role': 'reference'}]
        with patch('radar.sources._fetch_text', side_effect=fetch):
            result = sources.collect({'max_requests': 8}, seeds, NOW)
        row = next(p for p in result['projects'] if p['id'] == 'steam:123')
        self.assertEqual(row['discovery'], 'seed')
        self.assertEqual(row['stage'], 'demo')
        self.assertEqual(row['demo_parent_id'], 'steam:124')
        self.assertEqual(row['event_at'], '2026-10-23')
        with patch('radar.sources._fetch_text', side_effect=OSError('failed')):
            failed = sources.collect({'max_requests': 8}, seeds, NOW)
        self.assertEqual(failed['projects'], [])

    def test_bounds_request_count_and_reports_truncated_coverage(self):
        with patch('radar.sources._fetch_text', return_value=SEARCH) as network:
            result = sources.collect({'max_requests': 1}, [], NOW)
        self.assertLessEqual(network.call_count, 1)
        self.assertTrue(any(s['status'] in ('failed', 'partial') for s in result['sources']))

    def test_video_identity_preserves_video_identifier(self):
        self.assertEqual(sources.normalize_url('https://www.youtube.com/watch?v=abc123&t=10'),
                         'https://www.youtube.com/watch?v=abc123')
        self.assertNotEqual(sources.project_id('https://www.youtube.com/watch?v=abc123'),
                            sources.project_id('https://www.youtube.com/watch?v=other'))

    def test_source_counts_unique_projects_and_keeps_collection_source(self):
        with patch('radar.sources._fetch_text', return_value=SEARCH):
            result = sources.collect({'max_requests': 3}, [], NOW)
        self.assertEqual(result['sources'][0]['count'], 1)
        self.assertEqual(result['projects'][0]['collection_source'], 'Steam 公开发现')

    def test_redirect_to_private_host_never_connects(self):
        class Response:
            status = 302
            def getheader(self, key):
                return 'https://127.0.0.1/secret'
        with patch('radar.sources._network_target', wraps=sources._network_target) as target:
            with patch('radar.sources.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
                with patch('radar.sources._PinnedHTTPS') as connection:
                    connection.return_value.getresponse.return_value = Response()
                    with self.assertRaises(ValueError):
                        sources._fetch_text('https://itch.io/feed/new.xml')
                    self.assertEqual(connection.call_count, 1)

    def test_review_sample_uses_documented_filters_without_faking_missing_totals(self):
        payload = json.dumps({'response': {'reviews': [{'recommendationid': '77',
            'review': 'Fun but too short', 'language': 'english', 'voted_up': False,
            'timestamp_created': 1792792800}], 'cursor': 'next'}})
        sample = sources.parse_steam_reviews(payload, NOW)
        self.assertEqual(sample['sample_n'], 1)
        self.assertEqual(sample['reviews'][0]['review'], 'Fun but too short')
        self.assertIsNone(sample['total_matching'])
        self.assertEqual(sample['sampling']['coverage'], 'bounded_sample_not_all_reviews')
        with self.assertRaises(ValueError):
            sources.parse_steam_reviews('{"response":{}}', NOW)

    def test_comment_fetch_failure_preserves_project_and_records_sample_failure(self):
        detail = json.dumps({'123': {'success': True, 'data': {'name': 'Thing', 'type': 'game'}}})
        def fetch(url, **kwargs):
            if '/api/appdetails?' in url:
                return detail
            if '/GetAppReviews/' in url:
                params = json.loads(parse_qs(urlsplit(url).query)['input_json'][0])
                self.assertEqual(params['filter'], 1)
                self.assertEqual(params['languages'], ['all'])
                self.assertEqual(params['num_per_page'], 20)
                raise OSError('rate limited')
            raise OSError('unavailable')
        with patch('radar.sources._fetch_text', side_effect=fetch):
            result = sources.collect({'max_requests': 10}, [{'url': 'https://store.steampowered.com/app/123/'}], NOW)
        self.assertEqual(result['projects'][0]['review_sample']['status'], 'failed')
        self.assertEqual(result['projects'][0]['review_sample']['reviews'], [])
        self.assertEqual(result['projects'][0]['title'], 'Thing')

    def test_maker_title_uses_semantic_name_and_retains_separate_badges_as_evidence(self):
        # Structure checked on the live directory: badges are sibling widgets,
        # while an alternate game-cell anchor supplies schema.org name metadata.
        html = '''<a href="/app/123"><div class="home-app-card__title">
        <p class="tap-text-with-widget__contents home-app-card__title"><span>共命战线</span></p>
        <div class="tap-text-with-widget__widget"><span>测试</span><span>创意工坊</span></div></div></a>
        <a href="/app/124"><div class="text-with-tags app-title">
        <meta itemprop="name" content="名字含测试创意工坊"><span>名字含测试创意工坊</span>
        <span class="app-title-label-tag">测试</span><div class="craft-tag">创意工坊</div></div></a>'''
        rows = sources._maker(html, NOW)
        self.assertEqual([r['title'] for r in rows], ['共命战线', '名字含测试创意工坊'])
        self.assertIn('测试', rows[0]['evidence'][0]['body'])
        self.assertIn('创意工坊', rows[0]['evidence'][0]['body'])
        self.assertEqual(rows[0]['evidence'][0]['url'], sources.MAKER_DIRECTORY)

    def test_maker_unrecognized_title_structure_does_not_guess_from_badges(self):
        with self.assertRaises(ValueError):
            sources._maker('<a href="/app/123">共命战线<span>测试</span>创意工坊</a>', NOW)


if __name__ == '__main__':
    unittest.main()
