"""Application actions shared by the local HTTP UI and command line."""
from datetime import datetime
import hashlib
from html import escape
import json
import os
from pathlib import Path
import secrets
import threading
import uuid
from urllib.parse import urlsplit

from .store import Store, SHANGHAI, timestamp, text
from .feishu import build_card, send_report, review_labels


def render_report(report):
    def esc(value):
        return escape(str(value if value is not None else ''), quote=True)

    def link(url, label):
        try:
            parsed=urlsplit(str(url))
            if parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username:
                return '<span>链接待核实</span>'
        except ValueError:
            return '<span>链接待核实</span>'
        return '<a target="_blank" rel="noopener noreferrer" href="'+esc(url)+'">'+esc(label)+'</a>'

    def project(p):
        ev=p.get('evaluation') or {}
        evidence=''.join('<li><b>'+esc(e.get('type'))+' · '+esc(e.get('title'))+'</b><p>'+esc(e.get('body'))+'</p>'+link(e.get('url'),'回访这条证据 ↗')+'<p class="meta">观察时间：'+esc(e.get('observed_at') or '未记录')+'</p></li>' for e in p.get('evidence',[]))
        names=['传播','趣味','可复现','钩子','差异','体验']
        scores=ev.get('scores', [None]*6)
        reasons=ev.get('reasons', ['']*6)
        analysis=''.join('<tr><td>'+name+'</td><td>'+esc(scores[i] if scores[i] is not None else '未知')+'</td><td>'+esc(reasons[i])+'</td></tr>' for i,name in enumerate(names)) if ev else ''
        analysis_links=''.join('<li>'+link(u,u)+'</li>' for u in ev.get('evidence_urls',[]))
        metrics=esc(json.dumps(p.get('metrics',{}),ensure_ascii=False))
        metric_times=esc(json.dumps(p.get('metric_observed_at',{}),ensure_ascii=False))
        sample=p.get('review_sample')
        sample_html='<details><summary>评论采样记录（自动标签仅供复核）</summary><pre>'+esc(json.dumps(sample,ensure_ascii=False,indent=2))+'</pre></details>' if sample else '<p class="meta">评论尚未采样；不能据此断言负评主要来自内容少。</p>'
        last_attempt=p.get('review_sample_last_attempt') or {}
        if last_attempt.get('status')=='failed':
            sample_html+='<p class="meta">最近评论采样失败：'+esc(last_attempt.get('observed_at'))+'；已有成功样本仍按原采集时间展示。</p>'
        return ('<article><span class="tag">'+esc(p.get('source'))+' · '+esc(p.get('stage'))+'</span><h3>'+esc(p.get('title'))+'</h3><p>'+esc(p.get('summary'))+'</p>'
            +'<p>事件时间：'+esc(p.get('event_at') or '未知；采集日期不替代发生日期')+'<br>首次观察：'+esc(p.get('first_observed_at') or p.get('observed_at'))+'<br>最近采集：'+esc(p.get('observed_at'))+'</p>'
            +'<p>研究评分：'+esc(ev.get('score','未评估'))+' · 覆盖 '+esc(ev.get('coverage',0))+'%</p><table>'+analysis+'</table>'
            +'<p>核心机制：'+esc(ev.get('mechanism') or '待验证')+'</p><p>风险：'+esc(ev.get('risks') or '待核实，不代表没有风险')+'</p><p>下一步：'+esc(ev.get('next_step') or '补充实机和评估')+'</p>'
            +'<details><summary>评估证据链接</summary><ul>'+analysis_links+'</ul></details><details><summary>原始来源与证据</summary><ul>'+evidence+'</ul></details>'
            +'<details><summary>指标与各自采集时间</summary><p>'+metrics+'</p><p>'+metric_times+'</p></details>'+sample_html+link(p.get('url'),'查看原始作品页 ↗')+'</article>')

    sources=''.join('<tr><td>'+esc(s.get('name'))+'</td><td>'+esc(s.get('status'))+'</td><td>'+esc(s.get('message'))+'</td></tr>' for s in report.get('sources',[]))
    html=("""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>游戏雷达日报</title><style>body{max-width:1080px;margin:auto;padding:32px 22px;background:#f5f7f2;color:#243d31;font:15px/1.8 system-ui,"Microsoft YaHei",sans-serif}h1{font-size:32px}h2{margin-top:36px}.meta{color:#738171}article{border:1px solid #dfe7d8;background:white;border-radius:14px;padding:24px;margin:16px 0;overflow-wrap:anywhere}.tag{font-size:12px;color:#6e805e}a{color:#2e694b}table{border-collapse:collapse;width:100%;font-size:13px}td{border-bottom:1px solid #dce4d5;padding:12px;overflow-wrap:anywhere}details{margin:15px 0}p,pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>游戏雷达 · """+esc(report['date'])+'</h1><p class="meta">'+esc(report['id'])+' · '+esc(report['rule_version'])+' · '+esc(report['created_at'])+'</p><p>'+esc(report['summary'])+'</p><p>这是一份固定快照；后续反馈改变未来筛选，不覆盖历史证据。</p><h2>重点研究</h2>'
        +(''.join(project(p) for p in report['recommendations']) or '<p>本期没有满足证据与新信号要求的新增推荐。</p>')+'<h2>观察与参考</h2>'+(''.join(project(p) for p in report['observations']) or '<p>暂无候选。</p>')+'<h2>来源覆盖</h2><table>'+sources+'</table><p class="meta">观察项目不等同于高潜推荐；低评分、未发售本身均不是淘汰理由。</p></html>')

    if report.get('kind')=='collection':
        html=html.replace('这是一份固定快照；后续反馈改变未来筛选，不覆盖历史证据。','采集快照 · 尚未完成研究；最终日报生成前不可发送。',1)
    return html


def environment_config():
    mode=os.getenv('RADAR_FEISHU_MODE','webhook')
    return dict(mode=mode,webhook_url=os.getenv('FEISHU_WEBHOOK_URL',''),webhook_secret=os.getenv('FEISHU_WEBHOOK_SECRET',''),
                app_id=os.getenv('FEISHU_APP_ID',''),app_secret=os.getenv('FEISHU_APP_SECRET',''),chat_id=os.getenv('FEISHU_CHAT_ID',''),
                public_base_url=os.getenv('RADAR_PUBLIC_BASE_URL',''))


class Service:
    def __init__(self,data_dir,root=None,seeds=None,collector=None):
        self.root=Path(root or Path(__file__).resolve().parents[1])
        self.data_dir=Path(data_dir)
        self.store=Store(self.data_dir/'radar.sqlite3')
        self.csrf=secrets.token_urlsafe(32)
        self.feishu_config=environment_config()
        self.collector=collector
        self.collection_config={'request_timeout':8,'max_requests':15,'max_candidates':60}
        config_path=self.root/'config.json'
        if config_path.exists():
            local=json.loads(config_path.read_text(encoding='utf-8-sig'))
            self.collection_config.update(local.get('collection',{}))
        if seeds is None:
            seed_path=self.root/'data'/'seeds.json'
            seeds=json.loads(seed_path.read_text(encoding='utf-8-sig')) if seed_path.exists() else []
        self.seeds=seeds
        self.job_lock=threading.Lock()
        self.job=dict(id=None,status='idle',message='尚未运行采集')
        previous=self.store.settings().get('last_job') or {}
        if previous.get('status') in {'completed','failed'}:
            self.job=previous
        self.stop_event=threading.Event()
        self._seed_reference_library()

    def _seed_reference_library(self):
        if not self.seeds: return
        from .sources import normalize_url, project_id
        existing={p['id'] for p in self.store.projects()}
        records=[]
        for s in self.seeds:
            url=normalize_url(s['url'])
            pid=project_id(url)
            if pid in existing: continue
            records.append(dict(id=pid,title=s.get('title') or s.get('name'),url=url,source=s['source'],stage=s.get('stage','unknown'),
                summary=s.get('summary',''),tags=s.get('tags',[]),discovery='seed',event_type='参考案例',event_at=None,event_time_status='unknown',
                observed_at=s.get('observed_at','2026-10-23T00:00:00Z'),metrics={},
                evidence=[dict(type='参考资料，非本次采集',title='用户案例与前期核查',body=s.get('summary',''),url=url,observed_at=s.get('observed_at','2026-10-23T00:00:00Z'))]))
        self.store.upsert_collection({'projects':records,'sources':[]})

    def configured(self):
        c=self.feishu_config
        keys=['webhook_url'] if c.get('mode')=='webhook' else ['app_id','app_secret','chat_id']
        return c.get('mode') in {'webhook','app'} and all(c.get(k) for k in keys)

    def state(self):
        rules=self.store.rules()
        schedule_path=self.data_dir/'research-schedule.json'
        research_schedule=json.loads(schedule_path.read_text(encoding='utf-8')) if schedule_path.exists() else None
        return dict(csrf=self.csrf,projects=self.store.projects(),report=self.store.report(),reports=self.store.reports(),sources=self.store.sources(),
                    feedback=self.store.feedback(),rules=rules['rules'],rule_version=rules['version'],settings=self.store.settings(),
                    deliveries=self.store.deliveries(),service=dict(version='0.4',local_only=True,feishu_configured=bool(self.configured()),
                    feishu_mode=self.feishu_config['mode'],storage='SQLite 本机数据库，支持导出与备份',
                    analysis='助手定时查证与评估，支持网页纠正；未评估的候选保留未知',research_schedule=research_schedule,
                    limitations=['电脑休眠或服务停止时不会运行每日采集；开启后会补跑当天任务。','飞书接收聊天反馈及公网回调尚未接入；当前在网页反馈。','B站、抖音与 YouTube 首版接受链接线索，未启用全站视频搜索。']))

    def register(self,data):
        from .sources import normalize_url, project_id
        raw=text(data.get('url',''),2000)
        p=urlsplit(raw)
        host=p.hostname or ''
        allowed={'store.steampowered.com','itch.io','www.taptap.cn','www.bilibili.com','b23.tv','www.youtube.com','youtube.com','youtu.be','www.douyin.com','v.douyin.com'}
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or (host not in allowed and not host.endswith('.itch.io')):
            raise ValueError('请提供 Steam、itch.io、TapTap、B站、YouTube 或抖音的 HTTPS 公开链接')
        url=normalize_url(raw)
        pid=project_id(url)
        existing=next((x for x in self.store.projects() if x['id']==pid),None)
        title=text(data.get('title',''),200) or (existing or {}).get('title') or urlsplit(url).path.strip('/').split('/')[-1] or host
        note=text(data.get('note',''),4000)
        source='Steam' if host=='store.steampowered.com' else 'itch.io' if host.endswith('itch.io') else 'TapTap Maker' if host=='www.taptap.cn' else '视频线索'
        record=dict(id=pid,title=title,url=url,source=source,stage='unknown',summary='用户补充链接，玩法与传播待核实',
                    discovery='manual',tags=[],observed_at=timestamp(),event_at=None,event_type='用户线索',event_time_status='unknown',metrics={},
                    evidence=[dict(type='用户提供，待核实',title='手动补充线索',body=note or '请检查原始来源',url=url,observed_at=timestamp())])
        if existing:
            record={**existing,'evidence':existing.get('evidence',[])+record['evidence']}
            for key in ['flags','evaluation','preference_adjustment','matched_rules']: record.pop(key,None)
        self.store.upsert_collection({'projects':[record],'sources':[]})
        return {'project':record}

    def report(self,kind='final'):
        report=self.store.create_report(kind=kind)
        folder=self.data_dir/'reports'
        folder.mkdir(parents=True,exist_ok=True)
        (folder/(report['id']+'.html')).write_text(render_report(report),encoding='utf-8')
        return report

    def start_collection(self):
        if not self.job_lock.acquire(blocking=False): return {'job_id':self.job['id'],'status':'running'}
        self.job=dict(id=uuid.uuid4().hex,status='running',message='正在检查公开来源；无证据时保留未知',started_at=timestamp())
        threading.Thread(target=self._collect,daemon=True,name='radar-collector').start()
        return {'job_id':self.job['id'],'status':'running'}

    def _collect(self,send_after_collection=True):
        try:
            if self.collector is None:
                from .sources import collect
                collector=collect
            else: collector=self.collector
            # Refresh registered game pages; video URLs remain user evidence, never arbitrary fetches.
            seeds=[p for p in self.store.projects() if p.get('discovery') in {'seed','manual'} and p.get('source')!='视频线索']
            result=collector(self.collection_config,seeds,now=timestamp())
            for p in result.get('projects',[]):
                sample=p.get('review_sample') or {}
                for review in sample.get('reviews',[]):
                    review['tentative_labels']=review_labels(review.get('review') or review.get('text',''))
            self.store.upsert_collection(result)
            report=self.report(kind='final' if send_after_collection else 'collection')
            failed=sum(s.get('status')=='failed' for s in result.get('sources',[]))
            self.job.update(status='completed',message=f'采集结束：{len(result.get("projects",[]))} 条记录，{failed} 个来源失败；日报已保存',finished_at=timestamp(),report_id=report['id'])
            if send_after_collection and self.store.settings().get('auto_send') and self.configured(): self.send(report['id'])
        except Exception as error:
            self.job.update(status='failed',message='采集任务未完成；已有记录保留。错误类型：'+type(error).__name__,finished_at=timestamp())
        finally:
            self.store.set_settings({'last_job':dict(self.job)},internal=True)
            self.job_lock.release()

    def preview(self):
        report=self.store.report() or dict(id='尚未生成',date=datetime.now(SHANGHAI).strftime('%Y-%m-%d'),recommendations=[],observations=[],sources=self.store.sources(),rule_version=self.store.rules()['version'],summary='请先生成日报')
        base=self.feishu_config.get('public_base_url','').rstrip('/')
        return dict(card=build_card(report,base+'/reports/'+report['id']+'.html' if base else ''),report=report if report['id']!='尚未生成' else None,
                    report_id=report['id'] if report['id']!='尚未生成' else None,configured=bool(self.configured()),mode=self.feishu_config['mode'])

    def send(self,report_id):
        report_id=text(report_id,100)
        if not report_id: raise ValueError('发送需要明确的日报编号')
        report=self.store.report(report_id)
        if not report: raise ValueError('未找到日报')
        if report.get('kind')=='collection': return {'status':'draft','error':'这是采集快照；请完成查证后生成最终日报再发送'}
        if not self.configured(): return {'status':'unconfigured','error':'请先在服务端配置飞书凭据'}
        c=dict(self.feishu_config)
        base=c.get('public_base_url','').rstrip('/')
        c['report_url']=base+'/reports/'+report_id+'.html' if base else ''
        target=hashlib.sha256((c['mode']+'|'+c.get('webhook_url','')+'|'+c.get('chat_id','')).encode()).hexdigest()
        if not self.store.claim_delivery(report_id,target):
            prior=next((d for d in self.store.deliveries() if d['report_id']==report_id),{})
            return {'status':prior.get('status','unknown'),'message':'此日报已发送、正在发送或结果待确认；未重复发送'}
        result=send_report(report,c)
        return self.store.finish_delivery(report_id,target,result)

    def scheduler_tick(self,now=None):
        local=(now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        s=self.store.settings()
        today=local.strftime('%Y-%m-%d')
        if s.get('schedule_enabled') and local.strftime('%H:%M')>=s['daily_time'] and s.get('last_schedule_date')!=today and not self.job_lock.locked():
            self.store.set_settings({'last_schedule_date':today},internal=True)
            self.start_collection()

    def schedule_loop(self):
        while not self.stop_event.is_set():
            self.scheduler_tick()
            self.stop_event.wait(20)
