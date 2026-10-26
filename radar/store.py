"""Transactional local state; objective observations and preferences remain separate."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import hashlib
import json
import math
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
WEIGHTS = [25, 25, 20, 10, 10, 10]


def timestamp():
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def text(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('文字类型或长度不符合要求')
    return value.strip()


def score_evaluation(scores):
    if not isinstance(scores, list) or len(scores) != 6:
        raise ValueError('请填写六项评分；未知留空')
    for value in scores:
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 4):
            raise ValueError('评分必须在 0–4 之间；未知留空')
    coverage = sum(w for s, w in zip(scores, WEIGHTS) if s is not None)
    score = round(100 * sum(s*w/4 for s,w in zip(scores, WEIGHTS) if s is not None)/coverage) if coverage else None
    return dict(score=score, coverage=coverage, eligible=coverage >= 80 and all(s is not None and s >= 2 for s in scores[:3]) and score >= 75)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rules(version INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(report_id TEXT, target TEXT, payload TEXT NOT NULL, PRIMARY KEY(report_id,target));
            ''')
            if not db.execute('SELECT 1 FROM rules LIMIT 1').fetchone():
                db.execute('INSERT INTO rules(payload) VALUES (?)', (dumps(dict(rules=[], created_at=timestamp())),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _list(self, table, order='rowid'):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(f'SELECT payload FROM {table} ORDER BY {order}')]

    def upsert_collection(self, result):
        with self.connect() as db:
            for project in result.get('projects', []):
                old = db.execute('SELECT payload FROM projects WHERE id=?', (project['id'],)).fetchone()
                original = json.loads(old[0]) if old else {}
                merged = {**original, **project}
                # Retain provenance when a partial refresh has no replacement evidence.
                evidence={dumps(e):e for e in original.get('evidence',[])}
                evidence.update({dumps(e):e for e in project.get('evidence',[])})
                merged['evidence']=list(evidence.values())
                metrics=dict(original.get('metrics',{}))
                metric_times=dict(original.get('metric_observed_at',{}))
                for name,value in project.get('metrics',{}).items():
                    if value is not None:
                        metrics[name]=value
                        metric_times[name]=project.get('observed_at',timestamp())
                merged['metrics']=metrics
                merged['metric_observed_at']=metric_times
                if not project.get('tags'):
                    merged['tags']=original.get('tags',[])
                incoming_sample=project.get('review_sample')
                previous_sample=original.get('review_sample') or {}
                if incoming_sample:
                    merged['review_sample_last_attempt']=incoming_sample
                if previous_sample and (not incoming_sample or
                        (incoming_sample.get('status')!='ok' and previous_sample.get('status')=='ok')):
                    merged['review_sample']=previous_sample
                # Directory sightings must not erase an evidence-backed lifecycle event.
                if (project.get('event_type')=='directory_observation' and not parse_time(project.get('event_at'))
                        and parse_time(original.get('event_at'))):
                    for key in ('event_at','event_type','event_time_status','event_provenance'):
                        if key in original: merged[key]=original[key]
                elif parse_time(project.get('event_at')):
                    merged['event_provenance']=project.get('event_provenance') or {
                        'observed_at':project.get('observed_at'), 'source':project.get('collection_source') or project.get('source'),
                        'url':project.get('event_url') or project.get('url')}
                if not project.get('summary') and original.get('summary'):
                    merged['summary']=original['summary']
                if project.get('stage') in (None,'unknown') and original.get('stage'):
                    merged['stage']=original['stage']
                merged['first_observed_at'] = original.get('first_observed_at', project.get('observed_at', timestamp()))
                merged.setdefault('tags', [])
                merged.setdefault('evidence', [])
                db.execute('INSERT INTO projects VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (project['id'], dumps(merged)))
            for source in result.get('sources', []):
                db.execute('INSERT INTO sources VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload', (source['name'], dumps(source)))

    def sources(self):
        return self._list('sources')

    def feedback(self):
        return self._list('feedback', 'rowid DESC')

    def rules(self):
        with self.connect() as db:
            row = db.execute('SELECT version,payload FROM rules ORDER BY version DESC LIMIT 1').fetchone()
            return {**json.loads(row['payload']), 'version': f"rules-{row['version']}"}

    def _new_rules(self, db):
        effective = {}
        for row in db.execute('SELECT payload FROM feedback ORDER BY rowid'):
            f = json.loads(row[0])
            if not f['revoked'] and f['scope'] == 'preference':
                effective[f['rule']['tag'].casefold()] = {**f['rule'], 'feedback_id': f['id'], 'reason': f['note']}
        db.execute('INSERT INTO rules(payload) VALUES (?)', (dumps(dict(rules=list(effective.values()), created_at=timestamp())),))

    def projects(self):
        projects = self._list('projects')
        evaluations = {e['project_id']:e for e in self._list('evaluations')}
        feedback = [f for f in self.feedback() if not f['revoked']]
        rules = self.rules()['rules']
        for p in projects:
            p['evaluation'] = evaluations.get(p['id'])
            types = {f['type'] for f in feedback if f['project_id'] == p['id']}
            tags = {t.casefold() for t in p.get('tags', [])}
            matched = [r for r in rules if r['tag'].casefold() in tags]
            p['flags'] = dict(seen='seen' in types, followed='follow' in types, hidden='hide' in types,
                              fact_pending='fact' in types, excluded_by_rule=any(r['action']=='avoid' for r in matched))
            p['preference_adjustment'] = sum(10 if r['action']=='prefer' else -10 for r in matched)
            p['matched_rules'] = matched
        return sorted(projects, key=lambda p:(p['flags']['hidden'], -p['preference_adjustment'], p.get('title','')))

    def add_feedback(self, data):
        project_id = text(data.get('project_id'), 200)
        event_id = text(data.get('event_id'), 200)
        if not event_id:
            raise ValueError('缺少事件编号')
        kind, scope = data.get('type'), data.get('scope')
        note = text(data.get('note', ''), 4000)
        if kind not in {'accurate','quantity','boring','replicate','fact','seen','follow','hide','other'} or scope not in {'project','preference'}:
            raise ValueError('反馈类型或范围无效')
        if kind in {'fact','seen','follow','hide'} and scope != 'project':
            raise ValueError('事实、阅读和关注操作只能作用于当前项目')
        rule = None
        if scope == 'preference':
            raw = data.get('rule') or {}
            tag = text(raw.get('tag',''), 80)
            if raw.get('action') not in {'prefer','avoid'} or not tag or not note:
                raise ValueError('长期偏好需明确标签、优先或排除，以及原因')
            rule = {'action':raw['action'],'tag':tag}
        if kind == 'fact' and not note:
            raise ValueError('事实纠错请填写依据')
        canonical = dict(project_id=project_id,event_id=event_id,type=kind,scope=scope,note=note,rule=rule)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():
                raise ValueError('未找到项目')
            old = db.execute('SELECT payload FROM feedback WHERE event_id=?',(event_id,)).fetchone()
            if old:
                previous=json.loads(old[0])
                if any(previous.get(k)!=v for k,v in canonical.items()):
                    raise ValueError('相同事件编号对应不同内容，请重新提交')
                return previous
            if kind in {'follow','seen','hide'}:
                for row in db.execute('SELECT payload FROM feedback ORDER BY rowid DESC'):
                    active=json.loads(row[0])
                    if active['project_id']==project_id and active['type']==kind and not active['revoked']:
                        return active
            record = {**canonical,'id':uuid.uuid4().hex,'created_at':timestamp(),'revoked':False,'status':'applied' if scope=='preference' or kind in {'seen','follow','hide'} else 'recorded', 'storage':'sqlite'}
            db.execute('INSERT INTO feedback VALUES (?,?,?)',(record['id'],event_id,dumps(record)))
            if scope == 'preference':
                self._new_rules(db)
            return record

    def revoke(self, feedback_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM feedback WHERE id=?',(feedback_id,)).fetchone()
            if not row: raise ValueError('未找到反馈')
            f=json.loads(row[0])
            if not f['revoked']:
                f.update(revoked=True,revoked_at=timestamp(),status='revoked')
                db.execute('UPDATE feedback SET payload=? WHERE id=?',(dumps(f),feedback_id))
                if f['scope']=='preference': self._new_rules(db)
            return f

    def evaluate(self, data, author='user'):
        if author not in {'user','assistant'}: raise ValueError('评估来源无效')
        pid=text(data.get('project_id'),200)
        scores=data.get('scores')
        computed=score_evaluation(scores)
        reasons=data.get('reasons')
        urls=data.get('evidence_urls')
        if not isinstance(reasons,list) or len(reasons)!=6:
            raise ValueError('每个已评分维度需填写具体理由')
        reasons=[text(r,1500) for r in reasons]
        if any(s is not None and not r for s,r in zip(scores,reasons)):
            raise ValueError('已评分维度缺少理由')
        if not isinstance(urls,list) or len(urls)>20:
            raise ValueError('请提供最多 20 个证据链接')
        urls=[text(u,2000) for u in urls]
        if any(urlsplit(u).scheme!='https' or not urlsplit(u).hostname or urlsplit(u).username for u in urls):
            raise ValueError('证据链接应为有效 HTTPS 网址')
        if any(s is not None for s in scores) and not urls:
            raise ValueError('评分需要至少一个可回访证据链接')
        record=dict(id=uuid.uuid4().hex,project_id=pid,scores=scores,reasons=reasons,evidence_urls=urls,
                    mechanism=text(data.get('mechanism','')),risks=text(data.get('risks','')),
                    next_step=text(data.get('next_step','')),created_at=timestamp(),author=author,**computed)
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM projects WHERE id=?',(pid,)).fetchone(): raise ValueError('未找到项目')
            db.execute('INSERT INTO evaluations VALUES (?,?,?)',(record['id'],pid,dumps(record)))
        return record

    def settings(self):
        result=dict(schedule_enabled=False,daily_time='09:00',auto_send=False)
        with self.connect() as db:
            result.update({row['key']:json.loads(row['value']) for row in db.execute('SELECT * FROM settings')})
        return result

    def set_settings(self, values, internal=False):
        allowed={'schedule_enabled','daily_time','auto_send'} if not internal else set(values)
        if set(values)-allowed: raise ValueError('不支持的设置项')
        for k,v in values.items():
            if k in {'schedule_enabled','auto_send'} and type(v) is not bool: raise ValueError('开关值无效')
            if k=='daily_time' and (not isinstance(v,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v)):
                raise ValueError('每日时间应为 HH:MM')
        with self.connect() as db:
            for k,v in values.items():
                db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,dumps(v)))
        return self.settings()

    def create_report(self, now=None, kind='final'):
        if kind not in {'final','collection'}: raise ValueError('报告类别无效')
        now=now or timestamp()
        cutoff=parse_time(now)
        if cutoff is None: raise ValueError('报告时间无效')
        date=cutoff.astimezone(SHANGHAI).strftime('%Y-%m-%d')
        projects=self.projects()
        recent={}
        for old in self._list('reports'):
            if old.get('kind')=='collection': continue
            age=cutoff-parse_time(old['created_at'])
            if timedelta(0)<=age<timedelta(days=7):
                for p in old['recommendations']:
                    recent[p['id']]=(p.get('event_type'),p.get('event_at'))
        recommendations=[]
        observations=[]
        source_status={s['name']:s.get('status') for s in self.sources()}
        for p in projects:
            event=parse_time(p.get('event_at'))
            observed=parse_time(p.get('observed_at'))
            ev=p.get('evaluation') or {}
            flags=p['flags']
            fresh=event is not None and timedelta(0)<=cutoff-event<=timedelta(days=30)
            current=observed is not None and timedelta(0)<=cutoff-observed<=timedelta(hours=24) and source_status.get(p.get('collection_source') or p.get('source'))!='failed'
            new_signal=recent.get(p['id'])!=(p.get('event_type'),p.get('event_at'))
            if fresh and current and new_signal and p.get('discovery')!='seed' and ev.get('eligible') and not any(flags[k] for k in ['hidden','seen','fact_pending','excluded_by_rule']):
                recommendations.append(p)
            elif not flags['hidden']:
                observations.append(p)
        recommendations.sort(key=lambda p: (p['evaluation']['score']+p['preference_adjustment']),reverse=True)
        unique=[]
        parent_ids=set()
        for p in recommendations:
            identity=p.get('demo_parent_id') or p['id']
            if identity in parent_ids:
                observations.append(p)
            else:
                parent_ids.add(identity)
                unique.append(p)
        recommendations=unique
        observations= recommendations[5:]+observations
        observations.sort(key=lambda p:(p.get('discovery')=='seed',not bool(p.get('evaluation')),-p.get('preference_adjustment',0),p.get('title','')))
        recommendations=recommendations[:5]
        sources=self.sources()
        report=dict(id=date+'-'+uuid.uuid4().hex[:10],date=date,created_at=now,kind=kind,rule_version=self.rules()['version'],
                    recommendations=recommendations,observations=observations,sources=sources,
                    summary=f'本期 {len(recommendations)} 个重点研究项目；{len(observations)} 个观察/参考项目。' if recommendations else f'今天无新增高置信推荐；保留 {len(observations)} 个观察/参考项目，等待实机证据与评估。')
        with self.connect() as db:
            db.execute('INSERT INTO reports VALUES (?,?,?)',(report['id'],now,dumps(report)))
        return report

    def report(self, report_id=None):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM reports WHERE id=?',(report_id,)).fetchone() if report_id else db.execute('SELECT payload FROM reports ORDER BY rowid DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None

    def reports(self):
        return [dict(id=r['id'],date=r['date'],kind=r.get('kind','final'),created_at=r['created_at'],recommendation_count=len(r['recommendations']),observation_count=len(r['observations'])) for r in self._list('reports','rowid DESC')]

    def claim_delivery(self, report_id, target):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM reports WHERE id=?',(report_id,)).fetchone(): raise ValueError('未找到日报')
            row=db.execute('SELECT payload FROM deliveries WHERE report_id=? AND target=?',(report_id,target)).fetchone()
            old=json.loads(row[0]) if row else {}
            if old.get('status') in {'sending','sent','unknown'} or old.get('attempts',0)>=3: return False
            value=dict(status='sending',attempts=old.get('attempts',0)+1,updated_at=timestamp())
            db.execute('INSERT INTO deliveries VALUES (?,?,?) ON CONFLICT(report_id,target) DO UPDATE SET payload=excluded.payload',(report_id,target,dumps(value)))
            return True

    def finish_delivery(self, report_id,target,result):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM deliveries WHERE report_id=? AND target=?',(report_id,target)).fetchone()
            if not row: raise ValueError('缺少发送记录')
            value={**json.loads(row[0]),**result,'updated_at':timestamp()}
            db.execute('UPDATE deliveries SET payload=? WHERE report_id=? AND target=?',(dumps(value),report_id,target))
            return value

    def deliveries(self):
        with self.connect() as db:
            return [dict(report_id=r['report_id'],**json.loads(r['payload'])) for r in db.execute('SELECT * FROM deliveries')]

    def export(self):
        return dict(version='0.4',exported_at=timestamp(),projects=self.projects(),feedback=self.feedback(),
                    rules=self.rules(),rule_history=self._list('rules'),evaluations=self._list('evaluations'),
                    reports=self._list('reports'),sources=self.sources(),deliveries=self.deliveries())
