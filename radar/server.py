"""Loopback-only HTTP boundary. No arbitrary filesystem or source execution."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from .service import render_report


def make_server(service,port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version='GameRadar/0.4'

        def log_message(self,*args):
            pass

        def reply(self,status,body,content_type='application/json; charset=utf-8',download=None):
            raw=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8') if content_type.startswith('application/json') else body.encode('utf-8') if isinstance(body,str) else body
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download: self.send_header('Content-Disposition','attachment; filename="'+download+'"')
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self):
            hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in hosts:
                self.reply(403,{'error':'只允许从本机工作台访问'})
                return False
            return True

        def do_GET(self):
            if not self.valid_host(): return
            parsed=urlsplit(self.path)
            path=parsed.path
            if path=='/api/state': return self.reply(200,service.state())
            if path=='/api/job': return self.reply(200,dict(service.job))
            if path=='/api/feishu-preview': return self.reply(200,service.preview())
            if path=='/api/report':
                report=service.store.report(parse_qs(parsed.query).get('id',[None])[0])
                return self.reply(200 if report else 404,report or {'error':'未找到日报'})
            if path=='/api/export': return self.reply(200,service.store.export(),download='game-radar-backup.json')
            match=re.fullmatch(r'/reports/([A-Za-z0-9-]+)\.html',path)
            if match:
                report=service.store.report(match.group(1))
                return self.reply(200,render_report(report),'text/html; charset=utf-8') if report else self.reply(404,{'error':'未找到日报'})
            files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path in files:
                file=service.root/'web'/files[path]
                if file.is_file():
                    mime={'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}[files[path]]
                    return self.reply(200,file.read_bytes(),mime+'; charset=utf-8')
            self.reply(404,{'error':'未找到页面'})

        def do_POST(self):
            if not self.valid_host(): return
            host=self.headers['Host']
            if self.headers.get('Origin') not in (None,'http://'+host) or not secrets.compare_digest(self.headers.get('X-Radar-CSRF',''),service.csrf):
                return self.reply(403,{'error':'请求来源或会话已失效，请刷新工作台'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=128*1024: return self.reply(413,{'error':'请求大小无效或超过限制'})
                if not self.headers.get('Content-Type','').startswith('application/json'): return self.reply(415,{'error':'需要 JSON 请求'})
                self.connection.settimeout(10)
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict): raise ValueError('请求必须是对象')
                path=urlsplit(self.path).path
                if path=='/api/collect': result=service.start_collection()
                elif path=='/api/project': result=service.register(data)
                elif path=='/api/feedback': result=service.store.add_feedback(data)
                elif path=='/api/feedback/revoke': result=service.store.revoke(data.get('id'))
                elif path=='/api/evaluate': result=service.store.evaluate(data)
                elif path=='/api/report': result={'report':service.report()}
                elif path=='/api/settings':
                    if data.get('auto_send') and not service.configured(): raise ValueError('配置飞书后才可启用自动发送')
                    result=service.store.set_settings(data)
                elif path=='/api/send': result=service.send(data.get('report_id'))
                else: return self.reply(404,{'error':'未找到操作'})
                self.reply(200,result)
            except (ValueError,TypeError,KeyError) as error:
                message=str(error) if isinstance(error,ValueError) and not isinstance(error,json.JSONDecodeError) else '请求字段不完整或格式无效'
                self.reply(400,{'error':message[:300]})
            except Exception:
                self.reply(500,{'error':'本次操作未完成；请保留输入并检查本地服务'})

    return ThreadingHTTPServer(('127.0.0.1',port),Handler)
