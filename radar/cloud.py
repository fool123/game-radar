"""GitHub batch runner. Public cloud state is separate from local runtime data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import urllib.request
from .service import Service, render_report
from .store import timestamp, parse_time, SHANGHAI
from .feishu import send_report


def repository(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value or ''):
        raise ValueError('Repository must be owner/name')
    return value


def backup(service, destination):
    destination=Path(destination); destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.resolve()==service.store.path.resolve():raise ValueError('Backup must be a different file')
    with service.store.connect() as source:
        target=sqlite3.connect(destination)
        try:source.backup(target)
        finally:target.close()


def restore(source, directory):
    source=Path(source); directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    if source.exists():
        src=sqlite3.connect('file:'+source.resolve().as_posix()+'?mode=ro',uri=True)
        dest=sqlite3.connect(directory/'radar.sqlite3')
        try:src.backup(dest)
        finally:src.close();dest.close()


def fetch_issues(repo, token):
    repo=repository(repo)
    if not token:raise ValueError('GitHub token unavailable')
    rows=[]
    # Bounded but complete scans: on overflow abort instead of silently dropping feedback.
    for page in range(1,21):
        url=f'https://api.github.com/repos/{repo}/issues?state=all&per_page=100&page={page}'
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'game-radar','X-GitHub-Api-Version':'2022-11-28'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=20) as response:
            data=response.read(4*1024*1024+1)
            if len(data)>4*1024*1024:raise ValueError('Issue response too large')
            batch=json.loads(data)
        if not isinstance(batch,list):raise ValueError('Invalid GitHub response')
        rows.extend(batch)
        if len(batch)<100:return rows
    raise ValueError('Feedback scan exceeded budget; no partial sync applied')


def sync_feedback(service, issues, repo, editors=()):
    allowed={repository(repo).split('/')[0].casefold(),*(str(x).casefold() for x in editors)}
    mapping=dict(service.store.settings().get('cloud_issues',{})); applied=0; rejected=0
    for issue in sorted(issues,key=lambda x:x.get('number',0)):
        if 'pull_request' in issue or (issue.get('user') or {}).get('login','').casefold() not in allowed:continue
        number=str(issue.get('number'));previous=mapping.get(number)
        body=issue.get('body') or ''
        match=re.search(r'```json\s*\n(.*?)\n```',body,re.S)
        if issue.get('state')=='closed':
            if previous and previous.get('active'):
                service.store.revoke(previous['feedback_id']);previous['active']=False
            continue
        if not match:
            if previous and previous.get('active'):
                service.store.revoke(previous['feedback_id']);previous['active']=False
            continue
        try:
            payload=json.loads(match.group(1))
            if not isinstance(payload,dict) or payload.get('schema')!='game-radar-feedback/v1':continue
            fingerprint=hashlib.sha256(match.group(1).encode()).hexdigest()
            if previous and previous.get('active') and previous['fingerprint']==fingerprint:continue
            data={k:payload[k] for k in ('project_id','type','scope','note','rule') if k in payload}
            # A revision identity lets a closed then reopened unchanged issue apply again.
            data['event_id']='github:'+number+':'+hashlib.sha256((fingerprint+str(issue.get('updated_at'))).encode()).hexdigest()
            # Save new valid feedback before retiring the previous revision.
            entry=service.store.add_feedback(data)
            if previous and previous.get('active') and previous['feedback_id']!=entry['id']:
                service.store.revoke(previous['feedback_id'])
            mapping[number]=dict(feedback_id=entry['id'],fingerprint=fingerprint,active=True)
            applied+=1
        except (ValueError,TypeError,KeyError):rejected+=1
    service.store.set_settings({'cloud_issues':mapping},internal=True)
    return {'applied':applied,'rejected':rejected}


def daily_report(service, now=None):
    now=now or timestamp(); day=parse_time(now).astimezone(SHANGHAI).strftime('%Y-%m-%d')
    for item in service.store.reports():
        if item['date']==day and item.get('kind','final')=='final':return service.store.report(item['id'])
    return service.store.create_report(now)


PUBLIC_PROJECT_KEYS=('id','title','url','source','stage','summary','discovery','collection_source','observed_at','first_observed_at','event_at','event_type','event_time_status','event_precision','tags','metrics','metric_observed_at','demo_parent_id','flags','preference_adjustment')


def public_project(project):
    result={k:project[k] for k in PUBLIC_PROJECT_KEYS if k in project}
    # Local user notes are never copied into the website, even if passed accidentally.
    result['evidence']=[{k:e[k] for k in ('type','title','body','url','observed_at') if k in e}
                        for e in project.get('evidence',[]) if not any(s in e.get('type','') for s in ('用户提供','用户补充','私人'))]
    ev=project.get('evaluation')
    result['evaluation']={k:ev[k] for k in ('scores','reasons','evidence_urls','mechanism','risks','next_step','score','coverage','eligible','created_at','author') if k in ev} if ev and ev.get('author')=='assistant' else None
    return result


def public_report(report):
    if not report:return None
    result={k:report[k] for k in ('id','date','created_at','kind','rule_version','summary','sources') if k in report}
    for key in ('recommendations','observations'):result[key]=[public_project(p) for p in report.get(key,[])]
    return result


def public_state(service, repo):
    repo=repository(repo);rules=service.store.rules();sources=service.store.sources()
    ai=next((s.get('status') for s in sources if s.get('name')=='AI evidence analysis'),'unconfigured')
    latest=next((item for item in service.store.reports() if item.get('kind','final')=='final'),None)
    return dict(version='0.5-cloud',generated_at=timestamp(),repository=repo,
                projects=[public_project(p) for p in service.store.projects()],report=public_report(service.store.report(latest['id'])) if latest else None,
                reports=service.store.reports(),sources=sources,rules=[{k:r[k] for k in ('action','tag')} for r in rules['rules']],rule_version=rules['version'],
                cloud=dict(timezone='Asia/Shanghai',schedule='09:00',ai_status=ai,feishu_configured=service.configured(),feedback_url=f'https://github.com/{repo}/issues/new',actions_url=f'https://github.com/{repo}/actions',privacy='public'))


def publish(service, repo, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name in ('index.html','app.js','style.css'):shutil.copyfile(service.root/'cloud_web'/name,output/name)
    (output/'.nojekyll').write_text('',encoding='utf-8')
    (output/'data').mkdir(exist_ok=True);(output/'reports').mkdir(exist_ok=True)
    (output/'data'/'state.json').write_text(json.dumps(public_state(service,repo),ensure_ascii=False),encoding='utf-8')
    for item in service.store.reports():
        report=public_report(service.store.report(item['id']))
        (output/'reports'/(item['id']+'.html')).write_text(render_report(report),encoding='utf-8')


def delivery_target(service):
    c=service.feishu_config
    return hashlib.sha256((c['mode']+'|'+c.get('webhook_url','')+'|'+c.get('chat_id','')).encode()).hexdigest()


def claim_send(service, report_id, enabled=False):
    if not enabled or not service.configured():return None
    report=service.store.report(report_id)
    if not report or report.get('kind')=='collection':return None
    target=delivery_target(service)
    # Cloud never retries a claimed send, including failures, without manual investigation.
    if any(x['report_id']==report_id for x in service.store.deliveries()):return None
    if service.store.claim_delivery(report_id,target):return dict(report_id=report_id,target=target)
    return None


def deliver_claim(service,ticket,durable=False,sender=send_report):
    if not durable:raise ValueError('Commit the send claim to cloud-state before delivery')
    if not ticket:return {'status':'skipped'}
    if ticket.get('target')!=delivery_target(service):raise ValueError('Delivery target changed')
    report_id=ticket['report_id']
    prior=next((d for d in service.store.deliveries() if d['report_id']==report_id),{})
    if prior.get('status')!='sending':return prior or {'status':'skipped'}
    config=dict(service.feishu_config)
    base=config.get('public_base_url','').rstrip('/')
    config['report_url']=base+'/reports/'+report_id+'.html' if base else ''
    result=sender(public_report(service.store.report(report_id)),config)
    return service.store.finish_delivery(report_id,ticket['target'],result)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','send','publish'])
    parser.add_argument('--state',type=Path,default=Path('cloud-state/radar.sqlite3'))
    parser.add_argument('--runtime',type=Path,default=Path('cloud_runtime'))
    parser.add_argument('--output',type=Path,default=Path('site'))
    parser.add_argument('--repo',default=os.getenv('GITHUB_REPOSITORY','fool123/game-radar'))
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--feedback-only',action='store_true')
    parser.add_argument('--claim-committed',action='store_true')
    args=parser.parse_args();repository(args.repo)
    # Never let a deployment command accidentally expose the local user's database.
    local=Path(__file__).resolve().parents[1]/'runtime'
    if args.runtime.resolve()==local.resolve():raise ValueError('Use a separate cloud_runtime directory')
    if args.command=='prepare':restore(args.state,args.runtime)
    service=Service(args.runtime)
    if args.command=='prepare':
        if not args.offline:
            issues=fetch_issues(args.repo,os.getenv('GITHUB_TOKEN',''))
            sync_feedback(service,issues,args.repo,os.getenv('RADAR_EDITORS','').split(','))
            if not args.feedback_only:
                service.job_lock.acquire();service._collect(send_after_collection=False)
                from .cloud_research import enrich
                enrich(service)
        report=daily_report(service)
        ticket=claim_send(service,report['id'],not args.offline and not args.feedback_only and os.getenv('RADAR_AUTO_SEND')=='true')
        (args.runtime/'send-ticket.json').write_text(json.dumps(ticket),encoding='utf-8')
        backup(service,args.state)
        print(json.dumps(dict(report_id=report['id'],send_claimed=bool(ticket),projects=len(service.store.projects()))))
    elif args.command=='send':
        ticket_path=args.runtime/'send-ticket.json'
        ticket=json.loads(ticket_path.read_text(encoding='utf-8')) if ticket_path.exists() else None
        result=deliver_claim(service,ticket,args.claim_committed)
        backup(service,args.state)
        print(json.dumps({'delivery_status':result.get('status')}))
    else:
        publish(service,args.repo,args.output)
        print(json.dumps({'output':str(args.output),'projects':len(service.store.projects())}))


if __name__=='__main__':main()
