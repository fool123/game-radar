"""Bounded public observations. A failed fetch never becomes invented project data."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, urlencode, parse_qs
import xml.etree.ElementTree as ET


HOSTS = {'store.steampowered.com', 'api.steampowered.com', 'itch.io', 'www.taptap.cn'}
ITCH_FEED = 'https://itch.io/feed/new.xml'
MAKER_DIRECTORY = 'https://www.taptap.cn/tag/TapTap%E5%88%B6%E9%80%A0?sort=updated'


def normalize_url(url):
    if not isinstance(url, str) or len(url) > 4096 or re.search(r'[\x00-\x20\\\x7f]', url):
        raise ValueError('网址含无效字符')
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('仅支持不含凭证的标准 HTTPS 网址')
    host = p.hostname.encode('idna').decode('ascii').lower()
    if host == 'localhost' or '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
        raise ValueError('不支持本地网络网址')
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise ValueError('不支持私有地址')
    path = p.path or '/'
    query = ''
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com') and path == '/watch':
        video = parse_qs(p.query).get('v', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', video):
            raise ValueError('YouTube 链接缺少有效视频标识')
        host = 'www.youtube.com'
        query = urlencode({'v': video})
    if host == 'store.steampowered.com':
        match = re.match(r'^/app/(\d+)(?:/|$)', path)
        if match:
            path = '/app/' + match[1] + '/'
    elif host in ('www.taptap.cn', 'taptap.cn'):
        host = 'www.taptap.cn'
        match = re.match(r'^/app/(\d+)(?:/|$)', path)
        if match:
            path = '/app/' + match[1]
    else:
        path = path.rstrip('/') or '/'
    if ':' in host:
        host = '[' + host + ']'
    return urlunsplit(('https', host, path, query, ''))


def project_id(url):
    url = normalize_url(url)
    p = urlsplit(url)
    match = re.match(r'^/app/(\d+)/?$', p.path)
    if match and p.hostname == 'store.steampowered.com':
        return 'steam:' + match[1]
    if match and p.hostname == 'www.taptap.cn':
        return 'taptap:' + match[1]
    if p.hostname.endswith('.itch.io'):
        return 'itch:' + p.hostname[:-8] + p.path
    return 'url:' + hashlib.sha256(url.encode()).hexdigest()[:24]


def _network_target(url):
    normalize_url(url)
    host = urlsplit(url).hostname.lower()
    if host not in HOSTS and not (host.endswith('.itch.io') and host.count('.') == 2):
        raise ValueError('该主机不在公开采集白名单')
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(a[4][0] for a in answers))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('来源解析为非公开网络地址')
    return host, addresses[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _fetch_text(url, timeout=8, deadline=None, redirect_hook=None):
    deadline = deadline or time.monotonic() + timeout
    for attempt in range(4):
        if attempt and redirect_hook:
            redirect_hook()
        remaining = min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('采集时间预算已用尽')
        host, address = _network_target(url)
        p = urlsplit(url)
        target = quote(p.path or '/', safe='/%:@!$&\'()*+,;=-._~')
        if p.query:
            target += '?' + quote(p.query, safe='=&%+,;:/?@-._~')
        conn = _PinnedHTTPS(host, address, remaining)
        try:
            conn.request('GET', target, headers={'User-Agent': 'GameResearchRadar/0.1 (bounded public research)',
                                               'Accept-Encoding': 'identity', 'Accept': '*/*'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('重定向缺失目标')
                url = urljoin(url, location)
                # Validation is repeated before the next connection, including DNS.
                continue
            if response.status != 200:
                raise OSError('公开来源 HTTP ' + str(response.status))
            chunks, size = [], 0
            while True:
                remaining = min(timeout, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('采集时间预算已用尽')
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError('来源响应超过 2 MB 上限')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8-sig', errors='replace')
        finally:
            conn.close()
    raise ValueError('重定向超过上限')


def _plain(text):
    return re.sub(r'\s+', ' ', unescape(re.sub('<[^>]*>', ' ', str(text or '')))).strip()[:4000]


def _project(url, title, source, now, discovery='feed', stage='unknown', summary=''):
    url = normalize_url(url)
    return {'id': project_id(url), 'title': _plain(title)[:300], 'url': url, 'source': source,
            'stage': stage, 'summary': _plain(summary), 'observed_at': now,
            'event_type': 'directory_observation', 'event_at': None, 'event_time_status': 'unknown',
            'tags': [], 'evidence': [], 'metrics': {}, 'discovery': discovery}


def _evidence(row, url, title, body, now):
    row['evidence'].append({'type': 'public_page', 'title': title, 'body': _plain(body),
                            'url': url, 'observed_at': now})


def parse_itch_rss(text, entry_url, now):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('不接受 XML 外部实体或 DTD')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError('RSS 解析失败') from exc
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('来源不是受支持 RSS')
    rows = []
    for item in root.findall('./channel/item')[:60]:
        url, title = item.findtext('link'), item.findtext('title')
        if not url or not title:
            continue
        try:
            canonical = normalize_url(url)
            if not urlsplit(canonical).hostname.endswith('.itch.io'):
                continue
            row = _project(canonical, title, 'itch.io', now, summary=item.findtext('description'))
        except ValueError:
            continue
        row['event_type'] = 'feed_publication'
        try:
            date = parsedate_to_datetime(item.findtext('pubDate') or '')
            if date.tzinfo:
                row['event_at'] = date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                row['event_time_status'] = 'known'
        except (ValueError, TypeError, OverflowError):
            pass
        _evidence(row, entry_url, 'RSS 条目（发布时间不代表游戏发行时间）', row['summary'], now)
        rows.append(row)
    return rows


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.current, self.title_depth = [], None, 0
        self.name_capture = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a':
            self.current = {'url': a.get('href', ''), 'classes': a.get('class', ''), 'text': '', 'title': '', 'name': ''}
            self.name_capture = False
        if self.current:
            if tag == 'meta' and a.get('itemprop') == 'name':
                self.current['name'] = a.get('content', '')
            if tag == 'p' and {'tap-text-with-widget__contents', 'home-app-card__title'}.issubset(a.get('class', '').split()):
                self.name_capture = True
        if self.current and 'title' in a.get('class', '').split():
            self.title_depth = 1
        elif self.title_depth:
            self.title_depth += 1

    def handle_data(self, data):
        if self.current:
            self.current['text'] += data
            if self.name_capture:
                self.current['name'] += data
            if self.title_depth:
                self.current['title'] += data

    def handle_endtag(self, tag):
        if tag == 'p':
            self.name_capture = False
        if self.title_depth:
            self.title_depth -= 1
        if tag == 'a' and self.current:
            self.links.append(self.current)
            self.current = None
            self.title_depth = 0
            self.name_capture = False


def parse_steam_search(text, entry_url, now, stage='unknown'):
    parser = _Links()
    parser.feed(text)
    rows = []
    for link in parser.links:
        if 'search_result_row' not in link['classes'].split():
            continue
        try:
            url = normalize_url(link['url'])
            if urlsplit(url).hostname != 'store.steampowered.com' or not re.match(r'^/app/\d+/$', urlsplit(url).path):
                continue
            title = _plain(link['title'])
            if not title:
                continue
            row = _project(url, title, 'Steam', now, stage=stage)
            _evidence(row, entry_url, 'Steam 有范围公开搜索', link['text'], now)
            rows.append(row)
        except ValueError:
            continue
    if not rows and not any(marker in text for marker in ('search_resultsRows', 'search_results_count', 'No results')):
        raise ValueError('未识别 Steam 搜索结构，可能受限或页面已变更')
    return rows[:60]


def _steam_details(text, url, now):
    appid = project_id(url).split(':')[1]
    try:
        envelope = json.loads(text)[appid]
        if envelope.get('success') is not True:
            raise ValueError('Steam 未返回有效项目')
        data = envelope['data']
        if not isinstance(data.get('name'), str):
            raise ValueError('Steam 缺少项目名')
        if data.get('type') not in ('game', 'demo'):
            raise ValueError('Steam 结果不是游戏或 Demo')
        genres = data.get('genres') or []
        stage = 'demo' if data.get('type') == 'demo' else 'unknown'
        release = data.get('release_date') or {}
        if stage != 'demo' and release.get('coming_soon') is False:
            stage = 'released'
        if stage != 'demo' and any(g.get('id') == '70' for g in genres):
            stage = 'early_access'
        row = _project(url, data['name'], 'Steam', now, discovery='seed', stage=stage,
                       summary=data.get('short_description'))
        row['tags'] = [_plain(g.get('description')) for g in genres if isinstance(g, dict) and g.get('description')][:20]
        rawdate = release.get('date')
        if release.get('coming_soon') is False and isinstance(rawdate, str):
            for fmt in ('%d %b, %Y', '%b %d, %Y', '%d %B, %Y'):
                try:
                    row['event_at'] = datetime.strptime(rawdate, fmt).date().isoformat()
                    row['event_time_status'] = 'known'
                    row['event_type'] = 'release'
                    break
                except ValueError:
                    pass
        fullgame = data.get('fullgame') or {}
        if str(fullgame.get('appid', '')).isdigit() and stage == 'demo':
            row['demo_parent_id'] = 'steam:' + str(fullgame['appid'])
        _evidence(row, url, 'Steam 商店资料', 'type=' + str(data.get('type')) + '; release_date=' + str(rawdate) + '; ' + row['summary'], now)
        return row
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('Steam 项目响应格式无效') from exc


def _maker(text, now):
    parser = _Links()
    parser.feed(text)
    rows = []
    for link in parser.links:
        url = urljoin(MAKER_DIRECTORY, link['url'])
        try:
            url = normalize_url(url)
            if urlsplit(url).hostname != 'www.taptap.cn' or not re.fullmatch(r'/app/\d+', urlsplit(url).path):
                continue
            title = _plain(link['name'])
            if not title:
                continue
            row = _project(url, title, 'TapTap Maker', now)
            row['tags'] = ['TapTap制造']
            _evidence(row, MAKER_DIRECTORY, 'Maker 标签目录观察，排序不代表首次发布', link['text'], now)
            rows.append(row)
        except ValueError:
            continue
    if not rows:
        raise ValueError('Maker 目录未返回可解析项目；保留人工入口')
    return rows[:30]


REVIEW_SAMPLING = {'languages': ['all'], 'sort': 'recent', 'review_type': 'all',
                   'purchase_type': 'all', 'exclude_offtopic': True, 'page_limit': 1,
                   'page_size': 20, 'coverage': 'bounded_sample_not_all_reviews'}


def parse_steam_reviews(text, now):
    try:
        data = json.loads(text)
        data = data.get('response', data)
        if not isinstance(data.get('reviews'), list):
            raise ValueError('评论接口缺少 reviews 数组')
        rows = []
        for raw in data['reviews'][:20]:
            if not isinstance(raw, dict) or not isinstance(raw.get('review'), str):
                continue
            row = {key: raw.get(key) for key in ('recommendationid', 'language', 'voted_up', 'timestamp_created', 'timestamp_updated')}
            row['review'] = raw['review'][:12000]
            try:
                row['created_at'] = datetime.fromtimestamp(raw['timestamp_created'], timezone.utc).isoformat().replace('+00:00', 'Z')
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                row['created_at'] = None
            rows.append(row)
        return {'status': 'ok', 'observed_at': now, 'sample_n': len(rows), 'reviews': rows,
                'total_matching': data.get('total_matching'), 'query_summary': data.get('query_summary'),
                'sampling': dict(REVIEW_SAMPLING), 'next_cursor_available': bool(data.get('cursor')),
                'note': '最多一页；不是全量或随机样本；匿名接口可能缓存十分钟；不自动推断负评主因'}
    except (TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('评论响应格式无效') from exc


def collect(config, seeds, now=None):
    now = now or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    timeout = max(1, min(float(config.get('request_timeout', 8)), 15))
    maximum = max(1, min(int(config.get('max_requests', 15)), 20))
    limit = max(1, min(int(config.get('max_candidates', 60)), 100))
    deadline = time.monotonic() + min(85, timeout * maximum)
    requests = 0
    projects, statuses = {}, []
    reference_ids = set()
    for seed in seeds:
        try:
            reference_ids.add(project_id(seed['url']))
        except (ValueError, KeyError, TypeError):
            pass

    def claim_request():
        nonlocal requests
        if requests >= maximum or time.monotonic() >= deadline:
            raise TimeoutError('本次请求/时间预算已用尽，覆盖已截断')
        requests += 1

    def fetch(url):
        claim_request()
        return _fetch_text(url, timeout=timeout, deadline=deadline, redirect_hook=claim_request)

    def run(name, tasks):
        ids, errors, succeeded = set(), [], 0
        for task in tasks:
            try:
                rows = task()
                succeeded += 1
                for row in rows:
                    row['collection_source'] = name
                    if row['id'] in reference_ids:
                        row['discovery'] = 'seed'
                    if row['id'] not in projects and len(projects) >= limit:
                        continue
                    previous = projects.get(row['id'])
                    if previous and row['discovery'] != 'seed':
                        previous['evidence'].extend(row['evidence'])
                    else:
                        projects[row['id']] = row
                    ids.add(row['id'])
            except (OSError, ValueError, TimeoutError, http.client.HTTPException) as exc:
                # Do not expose URLs/credentials from arbitrary exception text.
                errors.append(type(exc).__name__ + ': ' + str(exc)[:200])
        status = 'partial' if errors and succeeded else ('failed' if errors else 'ok')
        statuses.append({'name': name, 'status': status, 'count': len(ids), 'checked_at': now,
                         'message': ('；'.join(errors) if errors else '有范围公开采集成功；非全站或完整历史')})

    searches = [('demo', {'category1': '10', 'sort_by': 'Released_DESC'}),
                ('unknown', {'filter': 'comingsoon', 'sort_by': 'Release_ASC', 'category1': '998'}),
                ('unknown', {'sort_by': 'Released_DESC', 'category1': '998'})]
    tasks = []
    for stage, params in searches:
        url = 'https://store.steampowered.com/search/?' + urlencode({**params, 'count': 20, 'ignore_preferences': 1, 'l': 'english', 'cc': 'us'})
        tasks.append(lambda url=url, stage=stage: parse_steam_search(fetch(url), url, now, stage))
    run('Steam 公开发现', tasks)
    run('itch.io RSS', [lambda: parse_itch_rss(fetch(ITCH_FEED), ITCH_FEED, now)])
    run('TapTap Maker 目录', [lambda: _maker(fetch(MAKER_DIRECTORY), now)])

    seed_tasks = []
    for seed in seeds[:10]:
        try:
            url = normalize_url(seed['url'])
            if project_id(url).startswith('steam:'):
                endpoint = 'https://store.steampowered.com/api/appdetails?' + urlencode({'appids': project_id(url)[6:], 'l': 'english', 'cc': 'us'})
                seed_tasks.append(lambda url=url, endpoint=endpoint: [_steam_details(fetch(endpoint), url, now)])
            elif urlsplit(url).hostname.endswith('.itch.io') or project_id(url).startswith('taptap:'):
                def page_seed(url=url):
                    text = fetch(url)
                    match = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
                    if not match:
                        raise ValueError('种子页面缺少可核验标题')
                    title = _plain(match[1])
                    if any(x in title.lower() for x in ('cloudflare', 'access denied', 'just a moment', '404', 'attention required')):
                        raise ValueError('种子页面受限或不存在')
                    source = 'itch.io' if urlsplit(url).hostname.endswith('.itch.io') else 'TapTap Maker'
                    row = _project(url, title, source, now, discovery='seed')
                    # Lifecycle classification only when explicitly present in page fields.
                    if re.search(r'>\s*Prototype\s*<', text, re.I):
                        row['stage'] = 'prototype'
                    elif re.search(r'>\s*Released\s*<', text, re.I):
                        row['stage'] = 'released'
                    _evidence(row, url, '已核验参考种子页面；非本次新发现', title, now)
                    return [row]
                seed_tasks.append(page_seed)
        except (ValueError, KeyError, TypeError):
            continue
    if seed_tasks:
        run('参考种子核验', seed_tasks)
    review_projects = sorted((p for p in projects.values() if p['id'].startswith('steam:')),
                             key=lambda p: p['id'] != 'steam:5245810')[:2]
    review_errors, review_count = [], 0
    for row in review_projects:
        params = {'appid': int(row['id'][6:]), 'filter': 1, 'languages': ['all'], 'cursor': '*',
                  'review_type': 0, 'purchase_type': 1, 'num_per_page': 20, 'filter_offtopic_activity': True}
        endpoint = 'https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/?' + urlencode({'input_json': json.dumps(params)})
        try:
            row['review_sample'] = parse_steam_reviews(fetch(endpoint), now)
            row['review_sample']['url'] = endpoint
            review_count += row['review_sample']['sample_n']
        except (OSError, ValueError, TimeoutError, http.client.HTTPException) as exc:
            row['review_sample'] = {'status': 'failed', 'sample_n': None, 'reviews': [],
                                    'sampling': dict(REVIEW_SAMPLING), 'observed_at': now,
                                    'error': type(exc).__name__ + ': ' + str(exc)[:200]}
            review_errors.append(row['id'] + ': ' + type(exc).__name__)
    statuses.append({'name': 'Steam 评论样本', 'status': ('partial' if review_errors and review_count else 'failed') if review_errors or not review_projects else 'ok',
                     'count': review_count, 'checked_at': now,
                     'message': '；'.join(review_errors) if review_errors else ('最多2个项目各20条；不是全量评论' if review_projects else '未获得可核验 Steam 项目，未请求评论')})
    statuses.extend([{'name': name, 'status': 'manual', 'count': 0, 'message': '仅支持人工补充公开来源链接', 'checked_at': now}
                     for name in ('B站 / 抖音',)])
    statuses.append({'name': 'YouTube', 'status': 'unconfigured', 'count': 0,
                     'message': '未接入授权 API；不生成指标或跨平台派生分数', 'checked_at': now})
    return {'projects': list(projects.values()), 'sources': statuses}
